<script module lang="ts">
  // VIEW-02 contracts (docs/design/PAQUETE_CLAUDE_DESIGN_VIEW_02.md sections 6 and 8). The nine TEA states are a closed
  // union; every one is reachable from the template and none is a sink.
  import type { CoreLoad, GateKey, MicroVmGate, PbxLease, PhysicalMemoryAllocation } from '../types/hypervisor';

  export type ViewState =
    | 'default'
    | 'hover'
    | 'active'
    | 'focus'
    | 'disabled'
    | 'loading'
    | 'empty'
    | 'error'
    | 'recovering';

  /** A gate as the ribbon shows it: the contract plus the attestation tag under the name. */
  export interface GateCard extends MicroVmGate {
    attestation: string;
  }

  /** A lease as the switchboard table shows it: the contract plus identity, tunnel, jitter and the status label. */
  export interface LeaseRow extends PbxLease {
    identity: string;
    tunnel: string;
    jitterMs: number;
    label: string;
    locked: boolean;
  }

  export interface LogLine {
    seq: number;
    ts: string;
    kind: 'RECV' | 'SEND' | 'SYNC' | 'ACK' | 'COMMIT';
    xid: string;
    text: string;
  }

  /** BLK-08: the continuous pointer hold the isolation trip demands, in ms [TARGET]. */
  export const ABORT_HOLD_MS = 1500;
  /** Section 8: recovery is non-absorbing and bounded; attempt k must satisfy k < RECOVERY_MAX. */
  export const RECOVERY_MAX = 3;
  /** Section 7.2: reactive updates are throttled to 10 Hz. */
  export const TICK_MS = 100;
  /** Section 7.2: the srfd estado poll period. */
  export const SRFD_POLL_MS = 5000;
  /** Ring commit stream rate the log module reproduces, messages per second [CITED view_02.html]. */
  export const UDS_MSG_RATE = 60;
  export const NUM_CORES = 20;
  export const GLIFO_GATE: Readonly<Record<MicroVmGate['status'], string>> = { nominal: '●', warning: '▲', tripped: '■' };

  export function agregadoCpu(cores: readonly CoreLoad[]): number {
    if (cores.length === 0) return 0;
    return cores.reduce((s, c) => s + c.percent, 0) / cores.length;
  }

  export interface SegmentosRam {
    soberanaPct: number;
    reservadoPct: number;
    disponiblePct: number;
    usadoPct: number;
  }

  /** The three segments of the RAM bar as percentages of the host ceiling; a zero ceiling yields zeros, never NaN. */
  export function segmentosRam(ram: PhysicalMemoryAllocation): SegmentosRam {
    const t = ram.techoTotalMib;
    if (!(t > 0)) return { soberanaPct: 0, reservadoPct: 0, disponiblePct: 0, usadoPct: 0 };
    const pct = (mib: number): number => Math.max(0, Math.min(100, (mib / t) * 100));
    return {
      soberanaPct: pct(ram.soberanaMib),
      reservadoPct: pct(ram.reservadoMib),
      disponiblePct: pct(ram.disponibleMib),
      usadoPct: pct(t - ram.disponibleMib),
    };
  }

  /** Hold progress 0..1 after `elapsedMs` of continuous hold; 1 at ABORT_HOLD_MS. */
  export function progresoDeHold(elapsedMs: number): number {
    if (!(elapsedMs > 0)) return 0;
    return Math.min(elapsedMs / ABORT_HOLD_MS, 1);
  }

  export function contarTripped(gates: readonly MicroVmGate[]): number {
    return gates.filter((g) => g.status === 'tripped').length;
  }

  export const CLAVES_GATE: readonly GateKey[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I'];
</script>

<script lang="ts">
  // Telemetry Master View (VIEW-02): telemetry strip and hardware isolation gauges, Svelte 5 runes only.
  //
  // GEOMETRY. The view lives INSIDE the canvas envelope: LIENZO_W x LIENZO_H at (GUTTER_IZQ, BARRA_H) of the shell,
  // split into the 40 px command bar (BARRA_H), the Bento canvas and the 28 px overlay strip (--overlay-foot-h,
  // rendered by TelemetryStrip.svelte). Every figure comes from ../geometria or app.css tokens (rule H-6).
  //
  // NO WEBGL HERE (DEC-4, BLK-07). The shell's single WebGL2 context belongs to App.svelte; this view never calls
  // getContext for WebGL. The 60 s sparkline is a 2D canvas.
  //
  // PERMANENT MOUNTING (BLK-04). The six modules, the strip and the state banners are mounted once; states switch
  // classes and visibility, values are written into fixed-width (ch) boxes. CLS = 0.000 [TARGET].
  //
  // DATA. The Go bridge (cockpit/app.go) exposes Socket and Enviar only: GetHostTelemetry, GetMicroVmGates,
  // TripMicroVmGate and GetPbxLeases (package section 7.1) are AUSENTE. The view probes for them, renders the
  // deterministic fixtures of section 9 otherwise and says FIXTURE on screen; the UDS health is real (srfd estado).
  import { untrack } from 'svelte';
  import TelemetryStrip from '../cards/TelemetryStrip.svelte';
  import { BARRA_H, DOM_H_SEXTO, DOM_W, GUTTER_IZQ, LIENZO_H, LIENZO_W, geometriaDeVentana } from '../geometria';
  import { RC_PARADA, SrfdError, causaDeRC, crearClienteSrfd, crearPuenteCockpit, transporteWails } from '../srfd';
  import type { AccionPbx, ExtensionPbx, GateOverride } from '../types/hypervisor';
  import type { ForensicRecordState, GpuBlackwellTelemetry, SurfaceCapsule } from '../types/hypervisor';

  interface Props {
    /** true on a bench page; the shell passes false (the view then sits at the canvas envelope). */
    standalone?: boolean;
    onGateTrip?: (gate: string) => void;
    onEmergencyHalt?: () => void;
  }

  let { standalone = false, onGateTrip = () => {}, onEmergencyHalt = () => {} }: Props = $props();

  /** app.css --overlay-foot-h [CITED]; the strip component reads the token, this constant sizes its row. */
  const FRANJA_H = 28;
  const MIB = 1 << 20;
  const KIB = 1 << 10;

  // ---- deterministic fixtures (section 9 and view_02.html [CITED]) ----------------------------------------------
  const CARGAS_BASE: readonly number[] = [18.2, 12.0, 8.4, 22.1, 14.6, 31.0, 5.2, 11.9, 16.3, 9.5, 4.1, 15.0, 28.4, 19.2, 7.8, 13.1, 24.5, 17.7, 10.3, 6.5];
  const fixtureCores = (): CoreLoad[] =>
    CARGAS_BASE.map((percent, coreId) => ({ coreId, cluster: Math.floor(coreId / 5), percent, freqGhz: 2.8 + (coreId % 5) * 0.1, tempC: 38.4 + (coreId % 7) * 0.7 }));
  const fixtureRam = (): PhysicalMemoryAllocation => ({ soberanaMib: 32768, reservadoMib: 49152, disponibleMib: 42024, techoTotalMib: 123944 });
  const fixtureGpu = (): GpuBlackwellTelemetry => ({ computePercent: 23.4, vramUsedBytes: 25878249472, vramTotalBytes: 103079215104, powerWatts: 142.0, powerCeilingWatts: 700.0, tempC: 44.8 });
  const GPU_CLOCK_MHZ = 2100;
  const GPU_TACH_RPM = 1850;
  const GPU_TEMP_MAX_C = 85.0;
  const fixtureGates = (): GateCard[] => [
    { key: 'A', name: 'Boot Integrity', status: 'nominal', attestation: 'PCR-0: 0xA4F2' },
    { key: 'B', name: 'VFIO IOMMU', status: 'nominal', attestation: 'GRP-14 ISO' },
    { key: 'C', name: 'Mem Isolation', status: 'nominal', attestation: 'EPT/NPT ON' },
    { key: 'D', name: 'Egress Cage', status: 'nominal', attestation: 'NFT TRAP DROP' },
    { key: 'E', name: 'Seccomp BPF', status: 'nominal', attestation: '42 Syscalls Deny' },
    { key: 'F', name: 'Cgroup V2', status: 'nominal', attestation: 'CPU.MAX 400k' },
    { key: 'G', name: 'Memfd Fence', status: 'nominal', attestation: 'SEAL_SHRINK/RW' },
    { key: 'H', name: 'PeerCred UDS', status: 'nominal', attestation: 'UID: 1000 GID: 1000' },
    { key: 'I', name: 'Audit Tap', status: 'nominal', attestation: 'RING: 0-DROP' },
  ];
  const CDP_PILLS: readonly string[] = ['auth:9222', 'human', 'attach', 'submit', 'harvest'];
  const fixtureLeases = (): LeaseRow[] => [
    { ext: 300, identity: 'supervisor_bridge', tunnel: 'ipc:///run/srf/bridge.sock', agentSlug: 'ag-master-0', role: 'operator', state: 'connected', epoch: 142, jitterMs: 0.021, label: 'LEASED', locked: false },
    { ext: 342, identity: 'browser_worker', tunnel: 'tcp://127.0.0.1:9222', agentSlug: 'ag-crawl-08', role: 'worker', state: 'connected', epoch: 142, jitterMs: 0.048, label: 'ACTIVE', locked: false },
    { ext: 405, identity: 'desktop_pilot', tunnel: 'xpra://:10022/display', agentSlug: 'ag-pilot-v2', role: 'worker', state: 'connected', epoch: 143, jitterMs: 0.034, label: 'STREAM', locked: false },
    { ext: 512, identity: 'pty_terminal', tunnel: '/dev/pts/3 [raw]', agentSlug: 'ag-sec-eval', role: 'auditor', state: 'connected', epoch: 140, jitterMs: 0.012, label: 'ATTACH', locked: false },
    { ext: 699, identity: 'emergency_monitor', tunnel: 'uds:///run/srf/sos.hub', agentSlug: 'daemon-sentry', role: 'monitor', state: 'on_hold', epoch: 140, jitterMs: 0.009, label: 'WATCH', locked: true },
  ];
  const fixtureForensic = (): ForensicRecordState => ({
    sessionId: 'rec-8f92a1c0-20260922-0540',
    targetXid: '0x0400001',
    sinceMonoMs: 142850,
    merkleRoot: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    verdict: 'NOMINAL',
  });
  const PLANTILLAS_LOG: ReadonlyArray<readonly [LogLine['kind'], string]> = [
    ['RECV', 'fd=9 memfd_create("xpra-fb", MFD_CLOEXEC) = 0'],
    ['SEND', 'SO_PEERCRED attest: valid sha256:7c9e81f'],
    ['RECV', 'cdp.Input.dispatchMouseEvent(type=mouseMoved, x=612, y=390)'],
    ['SYNC', 'memfd ring commit: 4096 bytes, seq=%SEQ% offset=0x000'],
    ['ACK', 'bus_integrity=100.000% delta_mono=%LAT%ms'],
    ['RECV', 'pty_stream data_len=24 ch=0x01 [stdout flush]'],
    ['COMMIT', 'gate_fence_verify(MEMFD_FENCE) == NOMINAL'],
  ];
  const LOG_LINEAS = 8;
  const SPARK_MUESTRAS = 60;
  const XID_BASE = 0x0400000;
  const CAPSULA_SLOT = 2; // CDP:9222 / XPRA:10022 [CITED view_02.html]: hypervisor.ts ports = base + slot
  const PUERTO_XPRA_BASE = 10020;
  const PUERTO_CDP_BASE = 9220;

  // ---- reactive state ------------------------------------------------------------------------------------------
  let viewState = $state<ViewState>('loading');
  let cores = $state<CoreLoad[]>([]);
  let ram = $state<PhysicalMemoryAllocation>(fixtureRam());
  let gpu = $state<GpuBlackwellTelemetry>(fixtureGpu());
  let gates = $state<GateCard[]>([]);
  let pbxLeases = $state<LeaseRow[]>([]);
  let forensic = $state<ForensicRecordState>(fixtureForensic());
  let udsConnected = $state(false);
  /** Round trip of the last srfd estado call, ms [MEASURED]; null before the first answer. */
  let udsLatencyMs = $state<number | null>(null);
  /** Ring commits counted over the last second [MEASURED on the stream this view renders]. */
  let udsMsgRate = $state(0);
  let abortHolding = $state(false);
  let abortHoldProgress = $state(0);
  let abortTimer = $state<number | null>(null);
  let errorMessage = $state('');
  let recoveryAttempt = $state(0);

  let fuente = $state<'fixture' | 'bridge'>('fixture');
  let udsAviso = $state('');
  let udsVivas = $state<number | null>(null);
  let selectedGate = $state<GateKey | null>(null);
  let streamPaused = $state(false);
  let log = $state<LogLine[]>([]);
  let spark = $state<number[]>([]);
  let utc = $state('--:--:--');
  let sparkEl = $state.raw<HTMLCanvasElement | null>(null);

  // ---- derived -------------------------------------------------------------------------------------------------
  const aggregateCpuPercent = $derived(agregadoCpu(cores));
  const trippedGatesCount = $derived(contarTripped(gates));
  const activeLeasesCount = $derived(pbxLeases.filter((l) => l.state === 'connected').length);
  const ramSeg = $derived(segmentosRam(ram));
  const ramUsagePercent = $derived(ramSeg.usadoPct);
  const holdPercentage = $derived(Math.round(abortHoldProgress * 100));
  const vramPct = $derived(gpu.vramTotalBytes > 0 ? (gpu.vramUsedBytes / gpu.vramTotalBytes) * 100 : 0);
  const powerPct = $derived(gpu.powerCeilingWatts > 0 ? (gpu.powerWatts / gpu.powerCeilingWatts) * 100 : 0);
  const tempPct = $derived((gpu.tempC / GPU_TEMP_MAX_C) * 100);
  const cpuTempC = $derived(cores.length === 0 ? 0 : cores.reduce((s, c) => s + c.tempC, 0) / cores.length);
  const gateSel = $derived(gates.find((g) => g.key === selectedGate) ?? null);
  const attestation = $derived(trippedGatesCount === 0 ? (gates.some((g) => g.status === 'warning') ? 'DEGRADED' : 'SECURE') : 'BREACH');
  const geo = $derived(geometriaDeVentana(DOM_W, DOM_H_SEXTO));
  const salud = $derived<SurfaceCapsule['health']>(viewState === 'error' ? 'breach' : trippedGatesCount > 0 ? 'degraded' : udsConnected ? 'healthy' : 'attaching');
  /** What TelemetryStrip.svelte shows at the bottom: the host as one capsule (spec 3.1 fields, real gate states). */
  const capsulaResumen = $derived<SurfaceCapsule>({
    id: `srf-ng-${CAPSULA_SLOT}`,
    name: 'ARM Grace (20c) + Blackwell GB10',
    archetype: 'browser',
    tier: 'kvm_hardware',
    mode: streamPaused ? 'read_only' : 'operator_controlled',
    health: salud,
    slot: CAPSULA_SLOT,
    xpraPort: PUERTO_XPRA_BASE + CAPSULA_SLOT,
    cdpPort: PUERTO_CDP_BASE + CAPSULA_SLOT,
    telemetry: {
      cpuPercent: aggregateCpuPercent,
      memoryBytes: (ram.techoTotalMib - ram.disponibleMib) * MIB,
      memoryLimitBytes: ram.techoTotalMib * MIB,
      networkRxRateBps: 0,
      networkTxRateBps: 0,
    },
    gates: {
      cdp: { auth: true, human: true, attach: true, submit: true, harvest: true },
      microvm: Object.fromEntries(CLAVES_GATE.map((k) => [k, (gates.find((g) => g.key === k)?.status ?? 'nominal') !== 'tripped'])) as Record<GateKey, boolean>,
    },
    recording: { recording: true, sessionId: forensic.sessionId, targetXid: forensic.targetXid, sinceMonoMs: forensic.sinceMonoMs, offsetMs: 12, verdict: forensic.verdict },
    pbx: null,
    poppedOut: false,
    occluded: false,
    createdAt: '2026-09-22T05:40:00Z',
  });

  const cliente = crearClienteSrfd(transporteWails());
  /** The rest of the bridge (cockpit/app.go stage 7.2): host telemetry, gate overrides, PBX phonebook. */
  const puenteCockpit = crearPuenteCockpit();
  /** Where the PBX table comes from: the phonebook through the bridge, or the section 9 fixture. */
  let fuenteLeases = $state<'fixture' | 'bridge'>('fixture');
  /** Poll period of GetHostTelemetry (procfs deltas): 1 Hz, paused with the stream. */
  const HOST_POLL_MS = 1000;

  // ---- state machine (section 8) --------------------------------------------------------------------------------
  export function transitionState(next: ViewState, err = ''): void {
    if (next === 'error') errorMessage = err === '' ? 'unspecified fault' : err;
    else if (next !== 'recovering') errorMessage = '';
    if (viewState === next) return;
    viewState = next;
  }

  function estadoDeReposo(): ViewState {
    if (pbxLeases.length === 0) return 'empty';
    if (streamPaused) return 'disabled';
    return 'default';
  }

  const interactivo = (): boolean => viewState === 'default' || viewState === 'hover' || viewState === 'active' || viewState === 'focus';
  function alEntrar(): void {
    if (viewState === 'default') transitionState('hover');
  }
  function alSalir(): void {
    if (viewState === 'hover' || viewState === 'active') transitionState('default');
  }
  function alPulsar(): void {
    if (interactivo()) transitionState('active');
  }
  function alSoltar(): void {
    if (viewState === 'active') transitionState('hover');
  }
  function alEnfocar(): void {
    if (interactivo()) transitionState('focus');
  }
  function alDesenfocar(): void {
    if (viewState === 'focus') transitionState('default');
  }

  /** Non-absorbing recovery, bounded at k < RECOVERY_MAX: re-polls srfd; success rests, failure returns to error. */
  export function retryRecovery(): void {
    if (recoveryAttempt >= RECOVERY_MAX) {
      transitionState('error', `recovery exhausted (k=${recoveryAttempt} >= ${RECOVERY_MAX}): RESET required`);
      return;
    }
    recoveryAttempt += 1;
    transitionState('recovering');
    void sondearUds().then((ok) => {
      if (viewState !== 'recovering') return;
      if (ok && trippedGatesCount === 0) {
        recoveryAttempt = 0;
        transitionState(estadoDeReposo());
      } else {
        transitionState('error', ok ? `${trippedGatesCount} gate(s) still tripped: ACKNOWLEDGE or RESET` : udsAviso === '' ? 'srfd did not answer' : udsAviso);
      }
    });
  }

  /** Leaves the error banner with the tripped gates kept on record (verdict DEGRADED). */
  export function acknowledgeError(): void {
    if (trippedGatesCount > 0 && forensic.verdict !== 'HALTED') forensic.verdict = 'DEGRADED';
    transitionState(estadoDeReposo());
  }

  /** Restores the fixtures: every gate nominal, every lease back, k = 0. */
  export function resetState(): void {
    if (puenteCockpit.disponible('resetGate')) void puenteCockpit.resetGate('ALL').catch(() => {});
    recoveryAttempt = 0;
    errorMessage = '';
    gates = fixtureGates();
    pbxLeases = fixtureLeases();
    forensic = { ...fixtureForensic(), sinceMonoMs: forensic.sinceMonoMs };
    selectedGate = null;
    streamPaused = false;
    transitionState('default');
  }

  function togglePausa(): void {
    streamPaused = !streamPaused;
    if (viewState !== 'error' && viewState !== 'recovering' && viewState !== 'loading') transitionState(estadoDeReposo());
  }

  /** RELEASE: the switchboard has no verb that releases every lease (AUSENTE): the table is cleared here only. */
  function releaseLeases(): void {
    pbxLeases = [];
    if (viewState !== 'error' && viewState !== 'recovering') transitionState(estadoDeReposo());
  }

  const ROL_PBX: Readonly<Record<string, LeaseRow['role']>> = { operator: 'operator', operador: 'operator', auditor: 'auditor', worker: 'worker', obrero: 'worker', monitor: 'monitor' };
  const ESTADO_PBX: Readonly<Record<string, LeaseRow['state']>> = { CONNECTED: 'connected', RINGING: 'ringing', ON_HOLD: 'on_hold', BARGED: 'barged', ON_HOOK: 'on_hook' };

  /** One phonebook extension (pbx.Extension) as a row of MOD-03. */
  function filaDeExtension(ext: ExtensionPbx): LeaseRow {
    const estado = ESTADO_PBX[(ext.estado ?? '').toUpperCase()] ?? 'on_hook';
    return {
      ext: ext.extension || ext.numero,
      agentSlug: ext.agente_slug || ext.slug,
      role: ROL_PBX[(ext.rol ?? '').toLowerCase()] ?? 'worker',
      state: estado,
      epoch: 0,
      identity: ext.slug,
      tunnel: ext.sala_actual !== undefined && ext.sala_actual !== '' ? ext.sala_actual : `pid ${ext.pid} · ${ext.modelo}`,
      jitterMs: 0,
      label: (ext.estado ?? 'ON_HOOK').toUpperCase(),
      locked: false,
    };
  }

  /** DIAL IN: the phonebook through the bridge (PbxLeases); the fixture only when the bridge lacks the method. */
  function dialLeases(): void {
    if (!puenteCockpit.disponible('pbxLeases')) {
      pbxLeases = fixtureLeases();
      fuenteLeases = 'fixture';
      if (viewState === 'empty') transitionState('loading');
      return;
    }
    if (viewState === 'empty') transitionState('loading');
    void puenteCockpit
      .pbxLeases()
      .then((exts) => {
        pbxLeases = exts.map(filaDeExtension);
        fuenteLeases = 'bridge';
        if (viewState === 'loading') transitionState(estadoDeReposo());
      })
      .catch((e: unknown) => transitionState('error', `PbxLeases: ${e instanceof Error ? e.message : String(e)}`));
  }

  /** JACK / TRUNK / MONITOR on one lease: sbd hold --off, sbd hold, sbd snoop through the bridge (cockpit/pbx.go). */
  function accionPbx(accion: AccionPbx, fila: LeaseRow): void {
    if (!puenteCockpit.disponible('pbxAccion')) {
      transitionState('error', `PbxAccion AUSENTE in this cockpit: ${accion} ${fila.agentSlug} not dispatched`);
      return;
    }
    void puenteCockpit
      .pbxAccion(accion, fila.agentSlug)
      .then((ext) => {
        const nueva = filaDeExtension(ext);
        pbxLeases = pbxLeases.map((l) => (l.ext === fila.ext ? { ...l, state: nueva.state, label: nueva.label } : l));
      })
      .catch((e: unknown) => transitionState('error', `PbxAccion ${accion} ${fila.agentSlug}: ${e instanceof Error ? e.message : String(e)}`));
  }

  /** The bridge's gate overrides (EstadoPuertas) folded over the fixture: a tripped override trips the gate here too. */
  function aplicarPuertas(overrides: readonly GateOverride[]): void {
    for (const o of overrides) {
      const g = gates.find((x) => x.key === o.key);
      if (g === undefined) continue;
      if (o.tripped && g.status !== 'tripped') {
        g.status = 'tripped';
        g.tripReason = o.reason ?? 'BRIDGE_OVERRIDE';
        g.trippedAtMonoMs = o.monoMs;
      } else if (!o.tripped && g.status === 'tripped' && g.tripReason !== undefined && g.tripReason !== 'MANUAL_OPERATOR_ABORT') {
        g.status = fixtureGates().find((x) => x.key === o.key)?.status ?? 'nominal';
        g.tripReason = undefined;
        g.trippedAtMonoMs = undefined;
      }
    }
  }

  async function sondearPuertas(): Promise<void> {
    if (!puenteCockpit.disponible('estadoPuertas')) return;
    try {
      aplicarPuertas(await puenteCockpit.estadoPuertas());
    } catch (e) {
      udsAviso = `EstadoPuertas: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  /** GetHostTelemetry at 1 Hz: cores and memory from procfs; the GPU stays a fixture while the bridge says AUSENTE. */
  async function sondearHost(): Promise<void> {
    if (streamPaused || !puenteCockpit.disponible('hostTelemetria')) return;
    try {
      const h = await puenteCockpit.hostTelemetria();
      cores = h.cores.map((c) => ({ ...c }));
      ram = { ...h.ram };
      if (h.gpu.fuente !== 'ausente') gpu = { ...h.gpu };
      fuente = 'bridge';
    } catch (e) {
      fuente = 'fixture';
      udsAviso = `GetHostTelemetry: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  function selectGate(key: GateKey): void {
    selectedGate = selectedGate === key ? null : key;
  }

  /** Trips one gate: the callback is told, the gate is marked, the machine enters error with recovery actions. */
  function tripGate(key: GateKey, reason = 'MANUAL_OPERATOR_TRIP'): void {
    const g = gates.find((x) => x.key === key);
    if (g === undefined || g.status === 'tripped') return;
    g.status = 'tripped';
    g.tripReason = reason;
    g.trippedAtMonoMs = forensic.sinceMonoMs;
    onGateTrip(key);
    const via = puenteCockpit.disponible('tripGate') ? 'TripGate(bridge override ledger)' : 'TripGate AUSENTE in this cockpit: local only';
    transitionState('error', `GATE ${key} (${g.name}) tripped: ${reason} · ${via}`);
    if (puenteCockpit.disponible('tripGate')) {
      void puenteCockpit.tripGate(key, reason).catch((e: unknown) => {
        errorMessage = `${errorMessage} · bridge: ${e instanceof Error ? e.message : String(e)}`;
      });
    }
  }

  // ---- isolation trip: 1.5 s continuous hold (BLK-08, DEF-03) --------------------------------------------------
  // Section 8: a TICK_MS interval, progress read from the elapsed wall clock (never from the tick count, which a
  // throttled timer would under-count), the trip at exactly ABORT_HOLD_MS of uninterrupted hold.
  let abortStart = 0;

  function pasoDeHold(): void {
    if (!abortHolding) return;
    const p = progresoDeHold(performance.now() - abortStart);
    abortHoldProgress = p;
    if (p >= 1) {
      if (abortTimer !== null) window.clearInterval(abortTimer);
      abortTimer = null;
      abortHolding = false;
      triggerEmergencyAbort();
    }
  }

  export function startAbortHold(ev: Event | null = null): void {
    ev?.preventDefault();
    if (abortHolding) return;
    abortHolding = true;
    abortStart = performance.now();
    abortHoldProgress = 0;
    abortTimer = window.setInterval(pasoDeHold, TICK_MS);
  }

  /** Releasing before ABORT_HOLD_MS resets the progress to 0 and trips nothing. */
  export function cancelAbortHold(): void {
    if (abortTimer !== null) window.clearInterval(abortTimer);
    abortTimer = null;
    abortHolding = false;
    abortHoldProgress = 0;
  }

  function alTeclaHold(ev: KeyboardEvent): void {
    if (ev.key !== ' ' && ev.key !== 'Enter') return;
    if (ev.type === 'keydown') {
      if (!ev.repeat) startAbortHold(ev);
      else ev.preventDefault();
    } else cancelAbortHold();
  }

  interface PuenteTelemetria {
    TripMicroVmGate?: (gateKey: string, reason: string) => Promise<unknown>;
    GetHostTelemetry?: () => Promise<unknown>;
  }
  const puente = (): PuenteTelemetria | null => {
    const app = (globalThis as { go?: { main?: { App?: PuenteTelemetria } } }).go?.main?.App;
    return app === undefined ? null : app;
  };

  /** The halt: every gate tripped, RC_PARADA reported, the parent told through onEmergencyHalt. */
  export function triggerEmergencyAbort(): void {
    abortHoldProgress = 1;
    for (const g of gates) {
      if (g.status !== 'tripped') {
        g.status = 'tripped';
        g.tripReason = 'MANUAL_OPERATOR_ABORT';
        g.trippedAtMonoMs = forensic.sinceMonoMs;
      }
    }
    forensic.verdict = 'HALTED';
    onEmergencyHalt();
    const p = puente();
    const via = p !== null && typeof p.TripMicroVmGate === 'function' ? 'TripMicroVmGate(ALL)' : 'TripMicroVmGate AUSENTE in cockpit/app.go: signalled to the shell only';
    transitionState('error', `EMERGENCY ISOLATION TRIP: rc ${RC_PARADA} (${causaDeRC(RC_PARADA)}) · ${via}`);
    if (p !== null && typeof p.TripMicroVmGate === 'function') {
      void p.TripMicroVmGate('ALL', 'MANUAL_OPERATOR_ABORT').catch((e: unknown) => {
        errorMessage = `${errorMessage} · bridge: ${e instanceof Error ? e.message : String(e)}`;
      });
    }
    abortHoldProgress = 0;
  }

  // ---- UDS health through the srfd client (the only bridge method that exists) --------------------------------
  async function sondearUds(): Promise<boolean> {
    const t0 = performance.now();
    try {
      const estado = await cliente.estado();
      udsLatencyMs = Math.round((performance.now() - t0) * 100) / 100;
      udsConnected = true;
      udsAviso = '';
      udsVivas = estado.vivas;
      ram = { ...ram, reservadoMib: estado.reservado_mib, disponibleMib: estado.disponible_mib, techoTotalMib: Math.max(ram.techoTotalMib, estado.reservado_mib + estado.disponible_mib) };
      return true;
    } catch (e) {
      udsConnected = false;
      udsVivas = null;
      udsAviso =
        e instanceof SrfdError
          ? e.codigo === 'rechazado'
            ? `srfd rc ${e.rc}: ${causaDeRC(e.rc)}`
            : e.message
          : e instanceof Error
            ? e.message
            : String(e);
      return false;
    }
  }

  // ---- 10 Hz stream (section 7.2): deterministic generator over the fixtures -----------------------------------
  let semilla = 0x2545f491;
  const azar = (): number => {
    // xorshift32: deterministic, no Math.random
    semilla ^= semilla << 13;
    semilla ^= semilla >>> 17;
    semilla ^= semilla << 5;
    return ((semilla >>> 0) % 1000) / 1000;
  };
  let seq = 19284;
  let tick = 0;
  let mensajesEnVentana = 0;
  let ventanaInicio = 0;

  function paso(): void {
    if (streamPaused) return;
    tick += 1;
    const ahora = new Date();
    utc = ahora.toISOString().slice(11, 19);
    const ts = ahora.toISOString().slice(11, 23);
    forensic.sinceMonoMs += TICK_MS;
    // cores: ±1.5 % around the fixture, clamped
    if (fuente === 'fixture') {
      for (const c of cores) {
        const base = CARGAS_BASE[c.coreId] ?? c.percent;
        c.percent = Math.max(0, Math.min(100, base + (azar() - 0.5) * 3));
      }
    }
    // ring commits: UDS_MSG_RATE per second in TICK_MS batches, LOG_LINEAS kept
    const porTick = Math.max(1, Math.round(UDS_MSG_RATE / (1000 / TICK_MS)));
    const nuevas: LogLine[] = [];
    for (let i = 0; i < porTick; i++) {
      seq += 1;
      const [kind, plantilla] = PLANTILLAS_LOG[seq % PLANTILLAS_LOG.length];
      const lat = udsLatencyMs === null ? 1.41 : udsLatencyMs;
      nuevas.push({ seq, ts, kind, xid: `0x${(XID_BASE + (seq % 4096)).toString(16).padStart(7, '0')}`, text: plantilla.replace('%SEQ%', String(seq)).replace('%LAT%', lat.toFixed(2)) });
    }
    log = [...log, ...nuevas].slice(-LOG_LINEAS);
    mensajesEnVentana += porTick;
    if (ahora.getTime() - ventanaInicio >= 1000) {
      udsMsgRate = mensajesEnVentana;
      mensajesEnVentana = 0;
      ventanaInicio = ahora.getTime();
    }
    if (tick % 10 === 0) spark = [...spark, aggregateCpuPercent].slice(-SPARK_MUESTRAS);
  }

  function inicializarTelemetryFixtures(): void {
    cores = fixtureCores();
    ram = fixtureRam();
    gpu = fixtureGpu();
    gates = fixtureGates();
    pbxLeases = fixtureLeases();
    forensic = fixtureForensic();
    log = [];
    spark = [aggregateCpuPercent];
    ventanaInicio = Date.now();
    fuente = puenteCockpit.disponible('hostTelemetria') ? 'bridge' : 'fixture';
  }

  // The mount is untracked (section 6.3): it writes what the template reads, so a tracked run would re-run itself.
  $effect(() => {
    untrack(() => {
      inicializarTelemetryFixtures();
    });
    const marcha = window.setInterval(() => untrack(paso), TICK_MS);
    const sondeo = window.setInterval(() => {
      void untrack(sondearUds);
      void untrack(sondearPuertas);
    }, SRFD_POLL_MS);
    const anfitrion = window.setInterval(() => void untrack(sondearHost), HOST_POLL_MS);
    void untrack(sondearUds).then(() => {
      if (untrack(() => viewState) === 'loading') transitionState(untrack(estadoDeReposo));
    });
    void untrack(sondearPuertas);
    void untrack(sondearHost);
    return () => {
      window.clearInterval(marcha);
      window.clearInterval(sondeo);
      window.clearInterval(anfitrion);
      cancelAbortHold();
    };
  });

  // the 'loading' state rests once leases have dialled in (empty -> loading -> default)
  $effect(() => {
    if (viewState === 'loading' && pbxLeases.length > 0 && cores.length > 0) {
      const id = window.setTimeout(() => untrack(() => transitionState(estadoDeReposo())), TICK_MS);
      return () => window.clearTimeout(id);
    }
  });

  // 60 s sparkline on a 2D context (never WebGL)
  $effect(() => {
    const el = sparkEl;
    const datos = spark;
    if (el === null) return;
    untrack(() => {
      const ctx = el.getContext('2d');
      if (ctx === null) return;
      const w = el.width;
      const h = el.height;
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = '#00e5ff';
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      const n = Math.max(datos.length, 2);
      const step = w / (SPARK_MUESTRAS - 1);
      datos.forEach((v, i) => {
        const x = (SPARK_MUESTRAS - n + i) * step;
        const y = h - ((Math.min(v, 40) / 40) * (h - 2) + 1);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  });

  const pct = (n: number, d = 1): string => n.toFixed(d).padStart(d + 3, '0');
  const gib = (mib: number): string => (mib / KIB).toFixed(1);
  const gibB = (bytes: number): string => (bytes / (KIB * MIB)).toFixed(1);
  const miles = (n: number): string => Math.round(n).toLocaleString('en-US');
</script>

<div
  class="vista02"
  class:anclada={!standalone}
  data-testid="telemetry-master-view"
  data-view-state={viewState}
  data-fuente={fuente}
  style:left={standalone ? null : `${GUTTER_IZQ}px`}
  style:top={standalone ? null : `${BARRA_H}px`}
  style:width="{LIENZO_W}px"
  style:height="{LIENZO_H}px"
>
  <!-- 1. GLOBAL COMMAND BAR (height: 40px = BARRA_H) -->
  <header class="mando" style:height="{BARRA_H}px" aria-label="Command bar">
    <div class="grupo">
      <span class="marca-v">OpenSuperficies</span>
      <span class="separador" aria-hidden="true"></span>
      <span class="mono cian">VIEW-02</span>
      <span class="mono etiqueta" title="ARM Grace (20c) + Blackwell GB10">srf-ng-{CAPSULA_SLOT}</span>
    </div>
    <div class="grupo">
      <span class="chip" data-uds={udsConnected ? 'up' : 'down'}>
        <span class="glifo" class:sano={udsConnected} class:apagado={!udsConnected}>{udsConnected ? '●' : '○'}</span>
        <span class="mono ancho-uds" title="/run/srfd/srfd.sock{udsAviso === '' ? '' : ` · ${udsAviso}`}">UDS: {udsConnected ? 'CONNECTED' : udsAviso === '' ? 'WAITING' : 'NO BRIDGE'}</span>
      </span>
      <span class="metrica"><span class="etiqueta">LATENCY</span><span class="mono cian ancho-lat">{udsLatencyMs === null ? 'AUSENTE' : `${udsLatencyMs.toFixed(2)}ms`}</span></span>
      <span class="metrica"><span class="etiqueta">RATE</span><span class="mono sano ancho-rate">{udsMsgRate} msg/s</span></span>
      <span class="metrica"><span class="etiqueta">BUS</span><span class="mono cian">100.000%</span></span>
      <span class="metrica"><span class="etiqueta">STATE</span><span class="mono ancho-estado" class:sano={interactivo()} class:aviso={viewState === 'loading' || viewState === 'recovering'} class:critico={viewState === 'error'} class:apagado={viewState === 'empty' || viewState === 'disabled'}>{viewState.toUpperCase()}</span></span>
    </div>
    <div class="grupo">
      <span class="reloj mono">{utc}<span class="etiqueta"> UTC</span></span>
      <button type="button" class="boton ancho-accion" onclick={togglePausa} aria-pressed={streamPaused} title="Pause or resume the 10 Hz stream (disabled state)">{streamPaused ? 'RESUME' : 'PAUSE'}</button>
      <button type="button" class="boton ancho-accion" onclick={pbxLeases.length === 0 ? dialLeases : releaseLeases} title="Release every lease (empty state) or dial them back">{pbxLeases.length === 0 ? 'DIAL IN' : 'RELEASE'}</button>
      <button
        type="button"
        class="trip"
        class:sosteniendo={abortHolding}
        onpointerdown={startAbortHold}
        onpointerup={cancelAbortHold}
        onpointerleave={cancelAbortHold}
        onpointercancel={cancelAbortHold}
        onkeydown={alTeclaHold}
        onkeyup={alTeclaHold}
        oncontextmenu={(e) => e.preventDefault()}
        aria-label="Trip isolation: hold for 1.5 seconds"
        title="Hold {ABORT_HOLD_MS} ms to trip every isolation gate"
      >
        <span class="glifo">■</span><span>TRIP ISOLATION</span><span class="pct mono">{holdPercentage}%</span>
        <span class="progreso" style:width="{holdPercentage}%"></span>
      </button>
    </div>
  </header>

  <!-- 2. BENTO CANVAS: 12 columns, gap 12px, three rows -->
  <main class="bento" style:height="{LIENZO_H - BARRA_H - FRANJA_H}px">
    <!-- MOD-01 -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <section class="modulo mod01" data-mod="01" aria-label="Substrate hardware cores and memory" tabindex="0" onpointerenter={alEntrar} onpointerleave={alSalir} onpointerdown={alPulsar} onpointerup={alSoltar} onfocus={alEnfocar} onblur={alDesenfocar}>
      <header class="cab">
        <span class="izq"><span class="titulo" title="ARM Neoverse V2 @ 3400 MHz">MOD-01 // SUBSTRATE HARDWARE CORES</span></span>
        <span class="der">
          <span class="etiqueta">AGGREGATE</span><span class="mono sano ancho-pct">{pct(aggregateCpuPercent)}%</span>
          <span class="etiqueta">TEMP</span><span class="mono ancho-pct">{cpuTempC.toFixed(1)}°C</span>
          <span class="etiqueta">60s</span><canvas class="spark" bind:this={sparkEl} width="64" height="16" aria-hidden="true"></canvas>
        </span>
      </header>
      <div class="nucleos" aria-label="20 Grace cores">
        {#each cores as c (c.coreId)}
          <div class="nucleo" data-core={c.coreId} class:caliente={c.percent >= 25}>
            <span class="fila"><span class="etiqueta">C{String(c.coreId).padStart(2, '0')}</span><span class="glifo sano">●</span></span>
            <span class="mono valor ancho-pct">{pct(c.percent)}%</span>
            <span class="barra-medida"><span class="relleno" class:cian={c.percent < 25} class:ambar={c.percent >= 25} style:width="{c.percent}%"></span></span>
          </div>
        {/each}
      </div>
      <div class="ram">
        <span class="fila">
          <span><span class="etiqueta">UNIFIED RAM CEILING</span> <span class="mono valor">{gib(ram.techoTotalMib)} GiB LPDDR5X</span></span>
          <span class="leyenda mono">
            <span class="cian">■ SOVEREIGN {gib(ram.soberanaMib)} GiB ({pct(ramSeg.soberanaPct)}%)</span>
            <span class="ambar">■ RESERVED {gib(ram.reservadoMib)} GiB ({pct(ramSeg.reservadoPct)}%)</span>
            <span class="sano">■ AVAILABLE {gib(ram.disponibleMib)} GiB ({pct(ramSeg.disponiblePct)}%)</span>
          </span>
        </span>
        <span class="segmentos" role="img" aria-label="RAM: sovereign {pct(ramSeg.soberanaPct)}%, reserved {pct(ramSeg.reservadoPct)}%, available {pct(ramSeg.disponiblePct)}%">
          <span class="seg cian" style:width="{ramSeg.soberanaPct}%"></span>
          <span class="seg ambar" style:width="{ramSeg.reservadoPct}%"></span>
          <span class="seg verde" style:width="{ramSeg.disponiblePct}%"></span>
        </span>
      </div>
    </section>

    <!-- MOD-04 -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <section class="modulo mod04" data-mod="04" aria-label="NVIDIA Blackwell GB10" tabindex="0" onpointerenter={alEntrar} onpointerleave={alSalir} onpointerdown={alPulsar} onpointerup={alSoltar} onfocus={alEnfocar} onblur={alDesenfocar}>
      <header class="cab">
        <span class="titulo">MOD-04 // NVIDIA BLACKWELL GB10</span>
        <span class="mono sano">● NVLINK: 900 GB/s</span>
      </header>
      <div class="tarjetas">
        <div class="tarjeta">
          <span class="fila"><span class="etiqueta">CORE COMPUTE</span><span class="mono cian">{GPU_CLOCK_MHZ} MHz</span></span>
          <span class="grande mono">{pct(gpu.computePercent)}%</span>
          <span class="barra-medida alta"><span class="relleno cian" style:width="{gpu.computePercent}%"></span></span>
          <span class="fila nota"><span>TENSOR FLOP: 14.8 TF</span><span>EFF: 99.1%</span></span>
        </div>
        <div class="tarjeta">
          <span class="fila"><span class="etiqueta">HBM3e VRAM</span><span class="mono cian">{gibB(gpu.vramTotalBytes)} GiB ALLOT</span></span>
          <span class="grande mono">{gibB(gpu.vramUsedBytes)} <span class="etiqueta">/ {gibB(gpu.vramTotalBytes)} GiB</span></span>
          <span class="barra-medida alta"><span class="relleno cian" style:width="{vramPct}%"></span></span>
          <span class="fila nota"><span>ALLOC: {pct(vramPct)}%</span><span>BW: 3.2 TB/s</span></span>
        </div>
        <div class="tarjeta">
          <span class="fila"><span class="etiqueta">THERM / TACH</span><span class="mono" class:sano={gpu.tempC < 70} class:aviso={gpu.tempC >= 70}>{gpu.tempC.toFixed(1)}°C / {GPU_TEMP_MAX_C.toFixed(1)}°C</span></span>
          <span class="grande mono">{GPU_TACH_RPM} <span class="etiqueta">RPM TACH</span></span>
          <span class="barra-medida alta"><span class="relleno" class:sano-bg={tempPct < 80} class:carmesi={tempPct >= 80} style:width="{tempPct}%"></span></span>
          <span class="fila nota"><span>FAN DUTY: 28% (HYSTERESIS LOCK)</span></span>
        </div>
        <div class="tarjeta">
          <span class="fila"><span class="etiqueta">POWER / CEILING</span><span class="mono valor">{gpu.powerWatts.toFixed(0)}W</span></span>
          <span class="grande mono ambar">{gpu.powerCeilingWatts.toFixed(0)}W <span class="etiqueta">PEAK RATED</span></span>
          <span class="barra-medida alta"><span class="relleno ambar" style:width="{powerPct}%"></span></span>
          <span class="fila nota"><span>VDD_SOC: 0.815V NOMINAL</span><span>{pct(powerPct)}%</span></span>
        </div>
      </div>
    </section>

    <!-- MOD-02 -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <section class="modulo mod02" data-mod="02" aria-label="MicroVM 9-axis isolation gates" tabindex="0" onpointerenter={alEntrar} onpointerleave={alSalir} onpointerdown={alPulsar} onpointerup={alSoltar} onfocus={alEnfocar} onblur={alDesenfocar}>
      <header class="cab">
        <span class="izq">
          <span class="titulo" title="MicroVM hardware isolation gates, 9-axis ribbon">MOD-02 // MICROVM ISOLATION GATES (9-AXIS)</span>
          <span class="pill ancho-att" class:sano={attestation === 'SECURE'} class:aviso={attestation === 'DEGRADED'} class:critico-solido={attestation === 'BREACH'}>ATTESTATION: {attestation}</span>
        </span>
        <span class="der">
          <span class="etiqueta">CDP PROXY</span>
          {#each CDP_PILLS as p (p)}<span class="pill sano">{p}</span>{/each}
        </span>
      </header>
      <div class="cinta">
        {#each gates as g (g.key)}
          <button type="button" class="gate" data-gate={g.key} data-status={g.status} class:sel={selectedGate === g.key} onclick={() => selectGate(g.key)} aria-pressed={selectedGate === g.key} title="{g.name}: {g.status}">
            <span class="fila"><span class="mono cian">GATE {g.key}</span><span class="glifo" class:sano={g.status === 'nominal'} class:aviso={g.status === 'warning'} class:critico={g.status === 'tripped'}>{GLIFO_GATE[g.status]}</span></span>
            <span class="nombre">{g.name}</span>
            <span class="etiqueta recorte">{g.attestation}</span>
          </button>
        {/each}
      </div>
      <div class="detalle mono">
        {#if gateSel === null}
          <span class="etiqueta">select a gate for its attestation and trip control</span>
        {:else}
          <span><span class="cian">GATE {gateSel.key}</span> {gateSel.name} · {gateSel.attestation} · <span class:sano={gateSel.status === 'nominal'} class:aviso={gateSel.status === 'warning'} class:critico={gateSel.status === 'tripped'}>{GLIFO_GATE[gateSel.status]} {gateSel.status.toUpperCase()}</span>{gateSel.tripReason ? ` · ${gateSel.tripReason} @ +${miles(gateSel.trippedAtMonoMs ?? 0)} ms` : ''}</span>
          <button type="button" class="boton mini peligro" onclick={() => tripGate(gateSel.key)} disabled={gateSel.status === 'tripped'}>TRIP GATE {gateSel.key}</button>
        {/if}
      </div>
    </section>

    <!-- MOD-05 -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <section class="modulo mod05" data-mod="05" aria-label="UDS FIFO ring commit log" tabindex="0" onpointerenter={alEntrar} onpointerleave={alSalir} onpointerdown={alPulsar} onpointerup={alSoltar} onfocus={alEnfocar} onblur={alDesenfocar}>
      <header class="cab">
        <span class="izq"><span class="titulo">MOD-05 // UDS FIFO RING COMMIT LOG</span><span class="mono sano ancho-rate">{udsMsgRate} msg/s</span></span>
        <span class="etiqueta">0-COPY MEMFD</span>
      </header>
      <div class="registro mono" aria-live="off">
        <span class="etiqueta cabecera-log">[UDS PEERCRED VALIDATION: TICK 0x7E1928 UID=1000 GID=1000 {udsConnected ? 'OK' : 'UNVERIFIED'}]</span>
        {#each { length: LOG_LINEAS } as _, i (i)}
          {@const l = log[i]}
          <span class="linea" data-kind={l?.kind ?? ''} class:cian={l?.kind === 'COMMIT' || l?.kind === 'RECV'} class:sano={l?.kind === 'ACK'}>{l === undefined ? ' ' : `${l.ts} >> ${l.kind.padEnd(6)} [${l.xid}] ${l.text}`}</span>
        {/each}
      </div>
    </section>

    <!-- MOD-03 -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <section class="modulo mod03" data-mod="03" aria-label="PBX switchboard leases" tabindex="0" onpointerenter={alEntrar} onpointerleave={alSalir} onpointerdown={alPulsar} onpointerup={alSoltar} onfocus={alEnfocar} onblur={alDesenfocar}>
      <header class="cab">
        <span class="izq"><span class="titulo">MOD-03 // PBX SWITCHBOARD OPERATOR LEASES [300..699]</span><span class="etiqueta mono">ACTIVE TRUNKS: {activeLeasesCount}/{pbxLeases.length}</span></span>
        <span class="mono sano">MAX JITTER: &lt;0.05ms</span>
      </header>
      <div class="tabla" class:vacia={pbxLeases.length === 0}>
        <div class="tfila cabt">
          <span>EXT</span><span>IDENTITY &amp; TUNNEL</span><span>HOLDER AGENT</span><span class="centro">STATUS</span><span class="dcha">JITTER</span><span class="dcha">TACTILE CONTROLS</span>
        </div>
        <div class="cuerpo-tabla">
          {#each pbxLeases as l (l.ext)}
            <div class="tfila" data-ext={l.ext} aria-disabled={l.locked} class:bloqueada={l.locked}>
              <span class="mono" class:cian={l.ext < 699} class:critico={l.ext >= 699}>{l.ext}</span>
              <span class="dos"><span class="valor">{l.identity}</span><span class="etiqueta recorte">{l.tunnel}</span></span>
              <span><span class="pill">{l.agentSlug}</span></span>
              <span class="centro"><span class="pill" class:sano={l.state === 'connected'} class:aviso={l.state === 'ringing' || l.state === 'on_hold'} class:apagado={l.state === 'on_hook'}>{l.state === 'connected' ? '●' : l.state === 'on_hook' ? '○' : '▲'} {l.label}</span></span>
              <span class="dcha mono valor">{l.jitterMs.toFixed(3)} ms</span>
              <span class="dcha controles">
                <button type="button" class="boton mini" disabled={l.locked} title="sbd hold --off (CONNECTED)" onclick={() => accionPbx('jack', l)}>JACK</button>
                <button type="button" class="boton mini" disabled={l.locked} title="sbd hold (ON_HOLD)" onclick={() => accionPbx('trunk', l)}>TRUNK</button>
                <button type="button" class="boton mini primario-suave" title="sbd snoop" onclick={() => accionPbx('monitor', l)}>MONITOR</button>
              </span>
            </div>
          {/each}
          <div class="vacio-leases" aria-hidden={pbxLeases.length !== 0}><span class="mono etiqueta">NO ACTIVE LEASES</span><button type="button" class="boton mini" onclick={dialLeases}>DIAL IN</button></div>
        </div>
        <div class="pie-tabla mono etiqueta"><span>SOURCE: {fuenteLeases === 'bridge' ? 'PHONEBOOK (sbd)' : 'FIXTURE'}</span><span>{fuenteLeases === 'bridge' ? `${pbxLeases.length} EXTENSIONS` : 'ZERO-PACKET LOSS VERIFIED'}</span></div>
      </div>
    </section>

    <!-- MOD-06 -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <section class="modulo mod06" data-mod="06" aria-label="Forensic audit record and kill switch" tabindex="0" onpointerenter={alEntrar} onpointerleave={alSalir} onpointerdown={alPulsar} onpointerup={alSoltar} onfocus={alEnfocar} onblur={alDesenfocar}>
      <header class="cab">
        <span class="titulo">MOD-06 // FORENSIC AUDIT RECORD</span>
        <span class="mono ancho-veredicto"><span class="glifo" class:sano={forensic.verdict === 'NOMINAL'} class:aviso={forensic.verdict === 'DEGRADED'} class:critico={forensic.verdict === 'HALTED'}>{forensic.verdict === 'NOMINAL' ? '●' : forensic.verdict === 'DEGRADED' ? '▲' : '■'}</span> VERDICT: {forensic.verdict}</span>
      </header>
      <div class="forense mono">
        <span class="fila borde"><span class="etiqueta">SESSION ID</span><span class="cian">{forensic.sessionId}</span></span>
        <span class="rejilla">
          <span><span class="etiqueta">TARGET XID </span><span class="valor">{forensic.targetXid}</span></span>
          <span><span class="etiqueta">OFFSET </span><span class="cian">12 ms</span></span>
          <span><span class="etiqueta">SINCE MONO </span><span class="valor ancho-mono">+{miles(forensic.sinceMonoMs)} ms</span></span>
          <span><span class="etiqueta">AUDIT RING </span><span class="sano">ZERO-DROP</span></span>
        </span>
        <span class="merkle"><span class="etiqueta">MERKLE ROOT</span><span class="cian recorte">{forensic.merkleRoot}</span></span>
      </div>
      <button
        type="button"
        class="trip grande"
        class:sosteniendo={abortHolding}
        onpointerdown={startAbortHold}
        onpointerup={cancelAbortHold}
        onpointerleave={cancelAbortHold}
        onpointercancel={cancelAbortHold}
        onkeydown={alTeclaHold}
        onkeyup={alTeclaHold}
        oncontextmenu={(e) => e.preventDefault()}
        aria-label="Emergency isolation kill switch: hold for 1.5 seconds"
        title="Hold {ABORT_HOLD_MS} ms to trip every isolation gate"
      >
        <span class="glifo">■</span><span>HOLD TO TRIP ISOLATION (1.5s)</span><span class="pct mono">{holdPercentage}%</span>
        <span class="progreso" style:width="{holdPercentage}%"></span>
      </button>
    </section>

    <!-- state banners: always mounted, absolutely positioned, one visible at a time (CLS 0) -->
    <div class="banner error" class:visible={viewState === 'error'} role="alert" aria-hidden={viewState !== 'error'}>
      <span class="titulo-b"><span class="glifo critico">▲</span> ERROR</span>
      <span class="texto">{errorMessage}</span>
      <span class="acciones">
        <button type="button" class="boton" onclick={retryRecovery} disabled={recoveryAttempt >= RECOVERY_MAX}>RETRY (k={recoveryAttempt}/{RECOVERY_MAX})</button>
        <button type="button" class="boton" onclick={acknowledgeError}>ACKNOWLEDGE</button>
        <button type="button" class="boton" onclick={resetState}>RESET</button>
      </span>
    </div>
    <div class="banner recuperando" class:visible={viewState === 'recovering'} aria-hidden={viewState !== 'recovering'}>
      <span class="titulo-b"><span class="glifo aviso">▲</span> RECONNECTING ({recoveryAttempt}/{RECOVERY_MAX})</span>
      <span class="texto">re-polling srfd estado over the UDS bridge…</span>
    </div>
    <div class="banner cargando" class:visible={viewState === 'loading'} aria-hidden={viewState !== 'loading'}>
      <span class="titulo-b"><span class="glifo aviso">▲</span> LOADING</span>
      <span class="esqueleto" aria-hidden="true"><i></i><i></i><i></i></span>
    </div>
  </main>

  <!-- 3. OVERLAY TELEMETRY STRIP (28px = --overlay-foot-h): TelemetryStrip.svelte plus the frame CLS chip -->
  <footer class="pie" style:height="{FRANJA_H}px" aria-label="Telemetry strip">
    <div class="franja-caja"><TelemetryStrip capsula={capsulaResumen} geometria={geo} visible={true} /></div>
    <span class="cls mono" title="DEC-13 {udsConnected ? 'SYNC OK' : 'NO BRIDGE'} · data source {fuente}"><span class="etiqueta">CLS</span><span class="sano">0.000</span><span class="punto-estado" class:sano={udsConnected} class:apagado={!udsConnected}></span><span class="etiqueta ancho-fuente">{fuente.toUpperCase()}</span></span>
  </footer>
</div>

<style>
  /* ---- tokens: docs/design/DESIGN.md Option A, Cyberpunk Obsidian [CITED] ---- */
  .vista02 {
    --c-void: var(--canvas-void, #07090e);
    --c-base: var(--surface-obsidian, #0e131f);
    --c-raised: var(--surface-cockpit, #161d2e);
    --c-overlay: var(--surface-overlay, #1c263b);
    --c-border: var(--border-subtle, #222d42);
    --c-border-strong: var(--border-strong, #334155);
    --c-cyan: var(--accent-cyan, #00e5ff);
    --c-cyan-hover: var(--accent-cyan-hover, #33ebff);
    --c-cyan-active: var(--accent-cyan-active, #00b8cc);
    --c-emerald: var(--accent-emerald, #00e676);
    --c-amber: var(--accent-amber, #ffb000);
    --c-crimson: var(--accent-crimson, #ff3355);
    --c-error-bg: #2d0a14;
    --c-destructive: var(--accent-destructive, #991b1b);
    --c-destructive-hover: #7f1d1d;
    --c-text: var(--text-polar, #f4f6fc);
    --c-text-2: var(--text-silver, #aab6c8);
    --c-text-off: var(--text-off, #7f8da3);
    --f-sans: var(--font-sans, 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif);
    --f-mono: var(--font-mono, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace);

    position: relative;
    z-index: 10;
    box-sizing: border-box;
    overflow: hidden;
    contain: strict;
    pointer-events: auto;
    background: var(--c-base); /* surface-base fills the frame: the instrument's pixel box stays the frame itself */
    color: var(--c-text);
    font: 500 12px/16px var(--f-sans);
    user-select: none;
    -webkit-font-smoothing: antialiased;
  }
  .vista02.anclada {
    position: absolute;
  }
  .vista02 *,
  .vista02 *::before,
  .vista02 *::after {
    box-sizing: border-box;
    box-shadow: none !important; /* pure flat */
    font-variant-numeric: tabular-nums;
    font-feature-settings: 'tnum' 1, 'zero' 1;
  }
  .vista02 button {
    font: inherit;
    color: inherit;
    cursor: pointer;
  }
  .vista02 button:focus-visible,
  .vista02 [tabindex]:focus-visible {
    outline: 2px solid var(--c-cyan);
    outline-offset: 2px;
  }
  .mono { font-family: var(--f-mono); font-weight: 500; }
  .etiqueta { color: var(--c-text-2); font: 500 10px/12px var(--f-mono); letter-spacing: 0.03em; }
  .valor { color: var(--c-text); }
  .cian { color: var(--c-cyan); }
  .ambar { color: var(--c-amber); }
  .sano { color: var(--c-emerald); }
  .aviso { color: var(--c-amber); }
  .critico { color: var(--c-crimson); }
  .apagado { color: var(--c-text-off); }
  .glifo { display: inline-block; width: 1em; text-align: center; font-family: var(--f-mono); }
  .recorte { display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .fila { display: flex; align-items: center; justify-content: space-between; gap: 6px; min-width: 0; }
  .fila.nota { color: var(--c-text-off); font: 500 9px/12px var(--f-mono); }
  .ancho-pct { display: inline-block; min-width: 6ch; text-align: right; }
  .ancho-uds { display: inline-block; min-width: 15ch; }
  .ancho-lat { display: inline-block; min-width: 8ch; }
  .ancho-rate { display: inline-block; min-width: 9ch; }
  .ancho-estado { display: inline-block; min-width: 10ch; }
  .ancho-att { min-width: 20ch; justify-content: center; }
  .ancho-veredicto { display: inline-block; min-width: 18ch; }
  .ancho-mono { display: inline-block; min-width: 12ch; }
  .ancho-fuente { display: inline-block; min-width: 7ch; }

  /* ---- 1. command bar: height 40px (= BARRA_H) ---- */
  .mando {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    height: 40px;
    padding: 0 12px;
    background: var(--c-base);
    border-bottom: 1px solid var(--c-border);
    white-space: nowrap;
    overflow: hidden;
  }
  .grupo { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .marca-v { font: 600 14px/20px var(--f-sans); letter-spacing: -0.01em; }
  .separador { width: 1px; height: 16px; background: var(--c-border); }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 24px;
    padding: 0 8px;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: 2px;
    font-size: 10px;
    line-height: 1;
  }
  .metrica { display: inline-flex; align-items: center; gap: 4px; font-size: 10px; }
  .reloj { min-width: 84px; text-align: right; font-size: 12px; }
  .boton {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    height: 32px;
    padding: 0 10px;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: 4px;
    color: var(--c-text-2);
    font: 500 11px/1 var(--f-mono);
    white-space: nowrap;
  }
  .boton:hover { color: var(--c-text); border-color: var(--c-border-strong); background: var(--c-overlay); }
  .boton:active { border-color: var(--c-cyan-active); color: var(--c-cyan-active); }
  .boton:disabled { opacity: 0.4; pointer-events: none; }
  .boton.mini { height: 24px; padding: 0 8px; font-size: 10px; }
  .boton.ancho-accion { width: 76px; } /* PAUSE/RESUME and RELEASE/DIAL IN share one box: the bar never reflows */
  .boton.primario-suave { border-color: var(--c-cyan); color: var(--c-cyan); }
  .boton.peligro { border-color: var(--c-crimson); color: var(--c-crimson); }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 18px;
    padding: 0 6px;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: 2px;
    color: var(--c-text);
    font: 500 10px/1 var(--f-mono);
    white-space: nowrap;
  }
  .pill.sano { color: var(--c-emerald); border-color: var(--c-emerald); }
  .pill.aviso { color: var(--c-amber); border-color: var(--c-amber); }
  .pill.apagado { color: var(--c-text-off); }
  /* a critical badge is a solid chip, #F4F6FC on #991B1B (7.69:1 AAA); never #FF3355 text alone */
  .pill.critico-solido { color: var(--c-text); background: var(--c-destructive); border-color: var(--c-destructive); }

  /* trip actuator: destructive, hold-to-confirm, cyan progress on the baseline */
  .trip {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 32px;
    padding: 0 12px;
    overflow: hidden;
    background: var(--c-destructive);
    border: 1px solid var(--c-destructive);
    border-radius: 4px;
    color: var(--c-text);
    font: 600 11px/1 var(--f-sans);
    letter-spacing: 0.02em;
    touch-action: none;
    white-space: nowrap;
  }
  .trip:hover { background: var(--c-destructive-hover); }
  .trip.sosteniendo { background: var(--c-destructive-hover); border-color: var(--c-cyan); }
  .trip .pct { display: inline-block; min-width: 4ch; text-align: right; font-size: 10px; color: var(--c-cyan); }
  .trip .progreso { position: absolute; left: 0; bottom: 0; height: 4px; width: 0; background: var(--c-cyan); }
  .trip.grande { width: 100%; height: 40px; justify-content: center; margin-top: auto; }

  /* ---- 2. Bento: 12 columns, 12 px gap, three fixed rows ---- */
  .bento {
    position: relative;
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    grid-template-rows: 232px 232px minmax(0, 1fr);
    gap: 12px;
    padding: 12px;
    background: var(--c-void);
    overflow: hidden;
  }
  .modulo {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
    min-height: 0;
    padding: 10px;
    overflow: hidden;
    background: var(--c-base);
    border: 1px solid var(--c-border);
    border-radius: 4px;
    contain: layout paint;
  }
  .modulo:hover { background: var(--c-raised); border-color: var(--c-border-strong); }
  .vista02[data-view-state='active'] .modulo:hover { background: var(--c-overlay); outline: 1px solid var(--c-cyan); outline-offset: -2px; }
  .vista02[data-view-state='disabled'] .modulo { opacity: 0.4; pointer-events: none; }
  .vista02[data-view-state='error'] .modulo { border-color: var(--c-crimson); }
  .vista02[data-view-state='recovering'] .modulo { border-color: var(--c-amber); }
  .mod01 { grid-column: 1 / 7; grid-row: 1; }
  .mod04 { grid-column: 7 / 13; grid-row: 1; }
  .mod02 { grid-column: 1 / 9; grid-row: 2; }
  .mod05 { grid-column: 9 / 13; grid-row: 2; }
  .mod03 { grid-column: 1 / 8; grid-row: 3; }
  .mod06 { grid-column: 8 / 13; grid-row: 3; }
  .cab {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    height: 24px;
    white-space: nowrap;
    overflow: hidden;
  }
  .cab .izq, .cab .der { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
  .titulo { color: var(--c-cyan); font: 600 10px/12px var(--f-mono); letter-spacing: 0.05em; text-transform: uppercase; }

  /* MOD-01 */
  .nucleos {
    display: grid;
    grid-template-columns: repeat(10, minmax(0, 1fr));
    grid-template-rows: repeat(2, 40px);
    gap: 4px;
    padding: 6px;
    background: var(--c-void);
    border: 1px solid var(--c-border);
    border-radius: 4px;
  }
  .nucleo {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 3px 4px;
    min-width: 0;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    font-size: 9px;
  }
  .nucleo .valor { font-size: 10px; font-weight: 600; }
  .barra-medida { display: block; width: 100%; height: 4px; background: var(--c-void); overflow: hidden; }
  .barra-medida.alta { height: 6px; border-radius: 2px; }
  .relleno { display: block; height: 100%; background: var(--c-cyan); }
  .relleno.cian { background: var(--c-cyan); }
  .relleno.ambar { background: var(--c-amber); }
  .relleno.carmesi { background: var(--c-crimson); }
  .relleno.sano-bg { background: var(--c-emerald); }
  .spark { width: 64px; height: 16px; background: var(--c-void); border: 1px solid var(--c-border); vertical-align: middle; }
  .ram { display: flex; flex-direction: column; gap: 4px; font-size: 10px; }
  .ram .fila { white-space: nowrap; overflow: hidden; }
  .ram .fila > span:first-child { flex: none; }
  .leyenda { display: inline-flex; gap: 10px; font-size: 9px; white-space: nowrap; overflow: hidden; }
  .segmentos { display: flex; height: 12px; width: 100%; background: var(--c-raised); border: 1px solid var(--c-border); border-radius: 2px; overflow: hidden; }
  .seg { display: block; height: 100%; }
  .seg.cian { background: var(--c-cyan); }
  .seg.ambar { background: var(--c-amber); }
  .seg.verde { background: color-mix(in srgb, var(--c-emerald) 40%, transparent); }

  /* MOD-04 */
  .tarjetas { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: repeat(2, minmax(0, 1fr)); gap: 8px; min-height: 0; flex: 1; }
  .tarjeta { display: flex; flex-direction: column; justify-content: space-between; gap: 3px; padding: 8px; min-width: 0; background: var(--c-raised); border: 1px solid var(--c-border); border-radius: 4px; overflow: hidden; }
  .tarjeta .grande { font-size: 16px; line-height: 20px; font-weight: 700; white-space: nowrap; }

  /* MOD-02 */
  .cinta { display: grid; grid-template-columns: repeat(9, minmax(0, 1fr)); gap: 4px; }
  .gate {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 2px;
    height: 56px;
    padding: 6px;
    min-width: 0;
    text-align: left;
    background: var(--c-raised);
    border: 1px solid var(--c-border);
    border-radius: 4px;
    font-size: 10px;
  }
  .gate:hover { border-color: var(--c-cyan); }
  .gate.sel { border-color: var(--c-cyan); background: var(--c-overlay); }
  .gate[data-status='tripped'] { border-color: var(--c-crimson); }
  .gate[data-status='warning'] { border-color: var(--c-amber); }
  .gate .nombre { color: var(--c-text); font: 500 10px/12px var(--f-sans); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .detalle { display: flex; align-items: center; justify-content: space-between; gap: 8px; height: 28px; padding: 0 8px; background: var(--c-void); border: 1px solid var(--c-border); border-radius: 4px; font-size: 10px; white-space: nowrap; overflow: hidden; }

  /* MOD-05 */
  .registro {
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    gap: 2px;
    flex: 1;
    min-height: 0;
    padding: 8px;
    background: var(--c-void);
    border: 1px solid var(--c-border);
    border-radius: 4px;
    color: var(--c-text-2);
    font-size: 10px;
    overflow: hidden;
  }
  .cabecera-log { padding-bottom: 4px; border-bottom: 1px solid var(--c-border); }
  .linea { display: block; height: 14px; line-height: 14px; overflow: hidden; white-space: pre; text-overflow: ellipsis; }

  /* MOD-03 */
  .tabla { display: flex; flex-direction: column; flex: 1; min-height: 0; background: var(--c-void); border: 1px solid var(--c-border); border-radius: 4px; overflow: hidden; }
  .tfila {
    display: grid;
    grid-template-columns: 44px minmax(0, 2.2fr) minmax(0, 1.4fr) 84px 72px 190px;
    align-items: center;
    gap: 6px;
    height: 44px;
    padding: 0 8px;
    border-bottom: 1px solid var(--c-border);
    font: 500 10px/12px var(--f-mono);
  }
  .tfila.cabt { height: 26px; background: var(--c-raised); color: var(--c-text-2); }
  .tfila:hover:not(.cabt) { background: color-mix(in srgb, var(--c-raised) 60%, transparent); }
  .tfila.bloqueada { opacity: 0.4; }
  .tfila .dos { display: flex; flex-direction: column; min-width: 0; }
  .tfila .dos .etiqueta { font-size: 9px; }
  .centro { text-align: center; justify-content: center; }
  .dcha { text-align: right; justify-content: flex-end; }
  .controles { display: inline-flex; gap: 4px; }
  .cuerpo-tabla { position: relative; flex: 1; min-height: 0; overflow: hidden; }
  .vacio-leases {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    border: 1px dashed var(--c-border);
    margin: 8px;
    visibility: hidden;
  }
  .tabla.vacia .vacio-leases { visibility: visible; }
  .pie-tabla { flex: none; display: flex; justify-content: space-between; height: 22px; align-items: center; padding: 0 8px; background: var(--c-base); border-top: 1px solid var(--c-border); }

  /* MOD-06 */
  .forense { display: flex; flex-direction: column; gap: 8px; padding: 8px; background: var(--c-raised); border: 1px solid var(--c-border); border-radius: 4px; font-size: 10px; }
  .forense .borde { padding-bottom: 6px; border-bottom: 1px solid var(--c-border); }
  .rejilla { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 8px; font-size: 11px; white-space: nowrap; }
  .rejilla .etiqueta, .forense .borde .etiqueta { margin-right: 4px; }
  .merkle { display: flex; align-items: center; gap: 8px; padding: 6px; background: var(--c-void); border: 1px solid var(--c-border); border-radius: 4px; min-width: 0; }
  .merkle .cian { font-weight: 700; letter-spacing: -0.01em; }

  /* banners */
  .banner {
    position: absolute;
    left: 48px;
    right: 48px;
    top: 48px;
    z-index: 5;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--c-raised);
    border: 2px solid var(--c-border-strong);
    border-radius: 4px;
    visibility: hidden;
    pointer-events: none;
  }
  .banner.visible { visibility: visible; pointer-events: auto; }
  .titulo-b { font: 700 13px/18px var(--f-mono); letter-spacing: 0.02em; }
  .banner .texto { color: var(--c-text-2); font: 400 12px/16px var(--f-mono); white-space: pre-wrap; word-break: break-word; }
  .banner .acciones { display: flex; gap: 8px; }
  .banner.error { border-color: var(--c-crimson); background: var(--c-error-bg); }
  .banner.error .titulo-b { color: var(--c-text); background: var(--c-destructive); align-self: flex-start; padding: 2px 8px; }
  .banner.recuperando { border-color: var(--c-amber); }
  .banner.recuperando .titulo-b { color: var(--c-amber); }
  .banner.cargando .titulo-b { color: var(--c-amber); }
  .esqueleto { display: flex; flex-direction: column; gap: 6px; }
  .esqueleto i { display: block; height: 10px; width: 60%; background: var(--c-overlay); }
  .esqueleto i:nth-child(2) { width: 85%; }
  .esqueleto i:nth-child(3) { width: 40%; }

  /* ---- 3. strip row: TelemetryStrip.svelte anchors itself to the bottom of this box ---- */
  .pie {
    position: relative;
    flex: none;
    background: var(--c-base);
    border-top: 1px solid var(--c-border);
    --overlay-foot-h: 28px;
  }
  .franja-caja { position: absolute; left: 0; top: 0; bottom: 0; width: calc(100% - 140px); }
  .cls {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    z-index: 2;
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    gap: 6px;
    width: 140px;
    padding: 0 12px;
    background: var(--c-base);
    border-left: 1px solid var(--c-border);
    font-size: 10px;
    white-space: nowrap;
  }
  .punto-estado { width: 8px; height: 8px; border-radius: 50%; background: var(--c-text-off); }
  .punto-estado.sano { background: var(--c-emerald); }

  @media (prefers-reduced-motion: reduce) {
    .vista02 * { transition: none !important; animation: none !important; }
  }
</style>
