// Canonical /state telemetry cells; no legacy srfd projection or view dependency.
const MIB = 1 << 20;

type MetricaCanonica = import('../types/hypervisor').Metric;
export interface CeldaSensor { texto: string; source: MetricaCanonica['source'] | 'none'; ts: number | null }
const FUENTES_SENSOR = new Set(['cgroup', 'procfs', 'srfd', 'sysfs', 'vadapt']);

/** A missing, malformed, future or stale sample is unknown; zero CPU is valid only with provenance. */
export function muestra(m: MetricaCanonica | null | undefined, docTs: number): number | null {
  if (m === null || typeof m !== 'object' || !FUENTES_SENSOR.has(m.source) ||
      !Number.isSafeInteger(m.value) || m.value < 0 || !Number.isSafeInteger(m.ts_mono_ms) ||
      !Number.isSafeInteger(docTs) || m.ts_mono_ms < 1 || m.ts_mono_ms > docTs || docTs - m.ts_mono_ms > 3000) return null;
  return m.value;
}

export function celdaCpu(m: MetricaCanonica | null | undefined, docTs: number): CeldaSensor {
  const value = muestra(m, docTs);
  if (value === null || m == null) return { texto: 'CPU —', source: 'none', ts: null };
  return { texto: `CPU ${Math.floor(value / 10)}.${value % 10}%`, source: m.source, ts: m.ts_mono_ms };
}

export function celdaRam(m: MetricaCanonica | null | undefined, docTs: number): CeldaSensor {
  const value = muestra(m, docTs);
  if (value === null || value <= 0 || m == null) return { texto: 'RAM —', source: 'none', ts: null };
  return { texto: `RAM ${Math.floor(value / MIB)} MiB`, source: m.source, ts: m.ts_mono_ms };
}

export function slotIdCanonico(slot: number): string { return `srf-ng-${slot}`; }
