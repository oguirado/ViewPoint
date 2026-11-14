// TypeScript contracts of the cockpit (ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md section 10).
// The shapes below are the specification's, verbatim, plus the shell-only types the srfd client
// and the preference bootstrap need. Nothing here computes anything.

export type IsolationTier = 'docker_l2' | 'kvm_hardware' | 'pty_jaula';
export type OperationMode = 'read_only' | 'operator_controlled' | 'agent_locked' | 'switching';
export type HealthStatus = 'healthy' | 'attaching' | 'degraded' | 'breach' | 'zombie';
export type Archetype = 'browser' | 'desktop' | 'microvm' | 'terminal';
export type WindowMode = 'tile_1_6' | 'column_1_3_tall' | 'column_1_3_dual';

export interface Geometry {
  mode: WindowMode;
  domWidth: number; // 1271 nominal
  domHeight: number; // 1064 or 2128 nominal
  canvasWidth: 1264;
  canvasHeight: 1024 | 2080;
  scaled: boolean; // true when domWidth < 1264
  scale: number; // 1 when pixel-exact
}

export interface SurfaceTelemetry {
  cpuPercent: number;
  memoryBytes: number;
  memoryLimitBytes: number;
  networkRxRateBps: number;
  networkTxRateBps: number;
}

export interface GateStatus {
  cdp?: { auth: boolean; human: boolean; attach: boolean; submit: boolean; harvest: boolean };
  microvm?: Record<'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H' | 'I', boolean>;
}

export interface RecordingState {
  recording: boolean;
  sessionId: string;
  targetXid: string;
  sinceMonoMs: number;
  offsetMs: number | null;
  verdict: string;
}

export interface PbxBinding {
  extension: number; // 300..699 for capsules
  role: string;
  holder: string | null; // agent slug holding the lease
  state: 'on_hook' | 'ringing' | 'connected' | 'on_hold' | 'barged';
  epoch: number;
}

export interface SurfaceCapsule {
  id: string; // "srf-ng-<slot>"
  name: string;
  archetype: Archetype;
  tier: IsolationTier;
  mode: OperationMode;
  health: HealthStatus;
  slot: number; // 0..79
  xpraPort: number; // 10020 + slot
  cdpPort: number | null; // 9220 + slot, browser only
  telemetry: SurfaceTelemetry;
  gates: GateStatus;
  recording: RecordingState;
  pbx: PbxBinding | null;
  poppedOut: boolean;
  occluded: boolean; // hidden, socket warm, NEVER unmounted
  createdAt: string;
}

// ---- srfd wire contract (src/surfacebook/manager/uds.go, Request / Response / SrfEstado / SurfaceInfo) ----

/** Verbs srfd's UDS accepts (Request.Comando). */
export type ComandoSrfd = 'estado' | 'iniciar' | 'detener' | 'mediacion' | 'listar';

/** Capsule classes srfd routes (manager.clasesValidas). */
export type ClaseSrfd = 'browser' | 'desktop' | 'microvm';

/** Response envelope: one JSON line per request. */
export interface RespuestaSrfd<T = unknown> {
  ok: boolean;
  rc: number;
  error?: string;
  datos?: T;
}

/** manager.SrfEstado: the six fixed fields of `estado`. */
export interface EstadoSrfd {
  estado: string;
  parada: string;
  vivas: number;
  reservado_mib: number;
  disponible_mib: number;
  soberana_mib: number;
}

/** manager.SurfaceInfo: one mediated slot as `listar` and `iniciar` return it. */
export interface InfoSuperficieSrfd {
  slot: number;
  clase: string;
  contenedor?: string;
  id?: string;
  puerto_xpra?: number;
  puerto_cdp?: number;
  lease_epoca?: number;
  pid?: number;
  reserva?: number;
  cpus?: number;
  estado: string;
}

// ---- shell preferences (resolved before first paint by the synchronous head bootstrap) ----

export type TemaCockpit = 'oscuro' | 'alto-contraste';

export interface PreferenciasCockpit {
  tema: TemaCockpit;
  /** Multiplier of the 16 px root font size; the layout uses rem, so it is a geometric preference. */
  escala: number;
}

// ---- VIEW-02 telemetry contracts (internal VIEW-02 design pack, section 6.1; not published) ----
// Host hardware, MicroVM isolation gates, PBX leases and the forensic record. The Go bridge exposes no method
// that returns them yet (cockpit/app.go: Socket, Enviar only): TelemetryMasterView renders deterministic fixtures
// and says so on screen until such a binding exists.

export interface CoreLoad {
  coreId: number; // 0..19
  cluster: number; // 0..3
  percent: number; // 0.0 .. 100.0
  freqGhz: number; // 2.4 .. 3.2
  tempC: number; // 35.0 .. 85.0
}

export interface PhysicalMemoryAllocation {
  soberanaMib: number; // 32768
  reservadoMib: number; // 49152
  disponibleMib: number; // 42024
  techoTotalMib: number; // 123944 (~121 GiB [MEASURED])
}

export interface GpuBlackwellTelemetry {
  computePercent: number; // 23.4
  vramUsedBytes: number; // 25878249472 (~24.1 GiB)
  vramTotalBytes: number; // 103079215104 (96.0 GiB)
  powerWatts: number;
  powerCeilingWatts: number;
  tempC: number; // 44.8
}

export type GateKey = 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H' | 'I';

export interface MicroVmGate {
  key: GateKey;
  name: string; // 'Boot Integrity', 'Memory Fence', ...
  status: 'nominal' | 'warning' | 'tripped';
  tripReason?: string;
  trippedAtMonoMs?: number;
}

export interface PbxLease {
  ext: number; // 300..699
  agentSlug: string; // 'agent-code-01', 'sub-audit-02'
  role: 'operator' | 'auditor' | 'worker' | 'monitor';
  state: 'on_hook' | 'ringing' | 'connected' | 'on_hold' | 'barged';
  epoch: number;
}

export interface ForensicRecordState {
  sessionId: string;
  targetXid: string;
  sinceMonoMs: number;
  merkleRoot: string;
  verdict: 'NOMINAL' | 'DEGRADED' | 'HALTED';
}

// ---- cockpit bridge contracts beyond srfd (cockpit/app.go stage 7.2: recording, PTY, gates, telemetry, PBX) ----

/** cockpit/grabacion.go RecordingState: RecordingState plus the supervisor's own fields. */
export interface RecordingSession extends RecordingState {
  /** idle | starting | recording | stopping | sealed | error */
  fase: 'idle' | 'starting' | 'recording' | 'stopping' | 'sealed' | 'error';
  slot: number;
  dir: string;
  display: string;
  /** '' until the recorder answers; then "completa" or "incompleta" (manifiesto.json). */
  manifiesto: string;
  /** The artifacts srf-grabar found on disk at t0 (4 when the session opened correctly). */
  artefactos: number;
  duracionMs: number;
  error?: string;
}

/** The four t0 artifacts of a session directory (grabador.go: video, events, flow, manifest). */
export interface ForensicArtifacts {
  video: string;
  eventos: string;
  flujo: string;
  manifiesto: string;
}

/** grabador.Candidato: one window of the recording display. */
export interface CandidatoGrabacion {
  xid: number;
  nombre: string;
  clase: string;
  wh: number[];
  mapeada: boolean;
  override_redirect: boolean;
}

/** cockpit/puertas.go GateOverride: an operator's override of one MicroVM gate, kept by the bridge. */
export interface GateOverride {
  key: GateKey;
  tripped: boolean;
  reason?: string;
  monoMs: number;
  wallTime?: string;
  fuente: string;
}

/** One agent the drawer attaches to: its id, the shell's stream URL and the shell it was asked to run. */
export interface TerminalAgentInfo {
  id: string;
  titulo: string;
  superficie: string;
  url: string;
  /** The directive typed into the shell once its stream opens ('' for a bare shell). */
  directiva: string;
}

/** cockpit/telemetria.go HostTelemetry. */
export interface HostTelemetry {
  cores: CoreLoad[];
  ram: PhysicalMemoryAllocation;
  gpu: GpuBlackwellTelemetry & { fuente: string };
  fuente: string;
  monoMs: number;
  wallTime: string;
}

/** manager.SrfMediacionReport: the census of the `mediacion` verb. */
export interface InformeMediacion {
  fuente: string;
  ajenos: number;
  mediadas: number;
  declaradas: number;
  observadas_total: number;
  observadas_docker: number;
  observadas_qemu: number;
  observadas_puertos: number;
  censo_docker_ok: boolean;
  censo_procfs_ok: boolean;
  contenedores_ajenos: string[];
  procesos_ajenos: number[];
  puertos_ajenos: number[];
  estado: string;
  errores?: string[];
}

/** pbx.Extension: one line of the switchboard phonebook. */
export interface ExtensionPbx {
  numero: number;
  extension: number;
  agente_slug: string;
  slug: string;
  modelo: string;
  rol: string;
  pid: number;
  estado: string;
  sala_actual?: string;
}

export type AccionPbx = 'jack' | 'trunk' | 'monitor';

// ---- F1-STATE frozen read-only contract (additive; section 10 shapes above are unchanged) ----
/** Canvas coordinates. JS-04: dom_x=x+3, dom_y=y+40+40*k;
 * k counts card bars above this leaf. */
export interface CapsuleRect { readonly x: number; readonly y: number; readonly w: number; readonly h: number }
export type SplitNode =
  | { readonly type: 'leaf'; readonly scale_q16?: number; readonly slot_id: string }
  | { readonly type: 'split'; readonly direction: 'vertical' | 'horizontal'; readonly ratio_q16: number; readonly left: SplitNode; readonly right: SplitNode };
export interface Metric { readonly source: 'cgroup' | 'procfs' | 'srfd' | 'sysfs' | 'vadapt'; readonly ts_mono_ms: number; readonly value: number }
export interface SlotBlock {
  readonly archetype: Archetype;
  readonly cdp: number | null;
  readonly geometry: CapsuleRect;
  readonly health: HealthStatus;
  readonly lease_epoch: number;
  readonly lease_holder: string | null;
  readonly lifecycle: 'booting' | 'ready' | 'busy' | 'stopping' | 'stopped' | 'failed';
  readonly mode: OperationMode;
  readonly ready: boolean;
  readonly rec_state: { readonly phase: 'idle' | 'starting' | 'recording' | 'stopping' | 'sealed' | 'error'; readonly store: 'ram' | 'disk' | null; readonly ttl_ms: number | null };
  readonly seq: number;
  readonly slot: number;
  readonly slot_id: string;
  readonly telemetry: { readonly cpu_permille: Metric | null; readonly mem_bytes: Metric | null };
  readonly uri: string;
  readonly workspace: number;
  readonly xpra_port: number;
}
export interface CockpitState {
  readonly capsules: readonly SlotBlock[];
  readonly host: string;
  readonly schema: 'os.cu.cockpit-state/v1' | 'os.cu.cockpit-state/v2';
  readonly seq: number;
  readonly ts_mono_ms: number;
  readonly window_mode: WindowMode;
  readonly workspaces: readonly { readonly layout: '1x1' | '1x2' | '2x2'; readonly n: number; readonly rect: CapsuleRect; readonly tree: SplitNode }[];
}
