//go:build cockpit

package main

import (
	"bufio"
	"fmt"
	"math"
	"math/bits"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Host telemetry for VIEW-02, read from procfs and sysfs on this host: per-core load from /proc/stat deltas between
// two calls, frequency from cpufreq, temperature from the thermal zones, memory from /proc/meminfo. The GPU is
// AUSENTE: no allowlisted tool reads the Blackwell GB10 (lanzador.go names no nvidia-smi), so its fields are zero
// and `fuente` says so. Nothing here is a fixture: a file that cannot be read makes the call fail.

const sysCpu = "/sys/devices/system/cpu"

// The sovereign floor of the governor (gobernador.go ReservaSoberana): 24 GiB [TARGET].
const soberanaMib = 24 * 1024

type CoreLoad struct {
	CoreID  int     `json:"coreId"`
	Cluster int     `json:"cluster"`
	Percent float64 `json:"percent"`
	FreqGhz float64 `json:"freqGhz"`
	TempC   float64 `json:"tempC"`
}

type PhysicalMemoryAllocation struct {
	SoberanaMib   uint64 `json:"soberanaMib"`
	ReservadoMib  uint64 `json:"reservadoMib"`
	DisponibleMib uint64 `json:"disponibleMib"`
	TechoTotalMib uint64 `json:"techoTotalMib"`
}

type GpuBlackwellTelemetry struct {
	ComputePercent    float64 `json:"computePercent"`
	VramUsedBytes     uint64  `json:"vramUsedBytes"`
	VramTotalBytes    uint64  `json:"vramTotalBytes"`
	PowerWatts        float64 `json:"powerWatts"`
	PowerCeilingWatts float64 `json:"powerCeilingWatts"`
	TempC             float64 `json:"tempC"`
	Fuente            string  `json:"fuente"`
}

type HostTelemetry struct {
	Cores    []CoreLoad               `json:"cores"`
	Ram      PhysicalMemoryAllocation `json:"ram"`
	Gpu      GpuBlackwellTelemetry    `json:"gpu"`
	Fuente   string                   `json:"fuente"`
	MonoMs   int64                    `json:"monoMs"`
	WallTime string                   `json:"wallTime"`
}

type muestraCpu struct {
	ocioso, total uint64
}

type telemetria struct {
	muCap    sync.Mutex
	prevCap  map[int]muestraCap
	mu       sync.Mutex
	inicio   time.Time
	anterior map[int]muestraCpu
}

func nuevaTelemetria() *telemetria { return &telemetria{inicio: time.Now()} }

// leerStat reads the per-core counters of /proc/stat (idle+iowait as idle, the sum as total).
func leerStat() (map[int]muestraCpu, error) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return nil, err
	}
	defer f.Close()
	salida := map[int]muestraCpu{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		campos := strings.Fields(sc.Text())
		if len(campos) < 8 || !strings.HasPrefix(campos[0], "cpu") || campos[0] == "cpu" {
			continue
		}
		n, err := strconv.Atoi(campos[0][3:])
		if err != nil {
			continue
		}
		var m muestraCpu
		for i, c := range campos[1:] {
			v, err := strconv.ParseUint(c, 10, 64)
			if err != nil {
				return nil, fmt.Errorf("/proc/stat %s: %w", campos[0], err)
			}
			m.total += v
			if i == 3 || i == 4 {
				m.ocioso += v
			}
		}
		salida[n] = m
	}
	return salida, sc.Err()
}

func leerMeminfo() (total, disponible uint64, err error) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		campos := strings.Fields(sc.Text())
		if len(campos) < 2 {
			continue
		}
		v, err := strconv.ParseUint(campos[1], 10, 64)
		if err != nil {
			continue
		}
		switch campos[0] {
		case "MemTotal:":
			total = v
		case "MemAvailable:":
			disponible = v
		}
	}
	if total == 0 || disponible == 0 {
		return 0, 0, fmt.Errorf("/proc/meminfo: MemTotal or MemAvailable missing")
	}
	return total, disponible, sc.Err()
}

func leerNumero(ruta string) (float64, bool) {
	b, err := os.ReadFile(ruta)
	if err != nil {
		return 0, false
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
	return v, err == nil
}

// temperaturaCpu is the first thermal zone whose type names the CPU/SoC, else the first zone; 0 when none is readable.
func temperaturaCpu() float64 {
	zonas, _ := filepath.Glob("/sys/class/thermal/thermal_zone*/type")
	sort.Strings(zonas)
	var primera float64
	var hayPrimera bool
	for _, z := range zonas {
		tipo, err := os.ReadFile(z)
		if err != nil {
			continue
		}
		t, ok := leerNumero(filepath.Join(filepath.Dir(z), "temp"))
		if !ok {
			continue
		}
		if !hayPrimera {
			primera, hayPrimera = t/1000, true
		}
		lt := strings.ToLower(string(tipo))
		if strings.Contains(lt, "cpu") || strings.Contains(lt, "soc") {
			return t / 1000
		}
	}
	return primera
}

// Leer samples the host; the first call takes two samples 100 ms apart so the load is a real delta.
func (t *telemetria) Leer() (*HostTelemetry, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.anterior == nil {
		primera, err := leerStat()
		if err != nil {
			return nil, err
		}
		t.anterior = primera
		time.Sleep(100 * time.Millisecond)
	}
	actual, err := leerStat()
	if err != nil {
		return nil, err
	}
	total, disponible, err := leerMeminfo()
	if err != nil {
		return nil, err
	}
	temp := temperaturaCpu()
	ids := make([]int, 0, len(actual))
	for id := range actual {
		ids = append(ids, id)
	}
	sort.Ints(ids)
	nucleos := len(ids)
	porCluster := 5 // the GB10 fixture of VIEW-02: 20 cores in 4 clusters
	if nucleos > 0 && nucleos%4 == 0 {
		porCluster = nucleos / 4
	}
	cores := make([]CoreLoad, 0, nucleos)
	for _, id := range ids {
		a, m := t.anterior[id], actual[id]
		var pct float64
		if m.total > a.total {
			pct = 100 * (1 - float64(m.ocioso-a.ocioso)/float64(m.total-a.total))
		}
		if pct < 0 {
			pct = 0
		}
		if pct > 100 {
			pct = 100
		}
		var ghz float64
		if khz, ok := leerNumero(filepath.Join(sysCpu, fmt.Sprintf("cpu%d", id), "cpufreq", "scaling_cur_freq")); ok {
			ghz = khz / 1e6
		}
		cores = append(cores, CoreLoad{CoreID: id, Cluster: id / porCluster, Percent: pct, FreqGhz: ghz, TempC: temp})
	}
	t.anterior = actual
	usadoMib := (total - disponible) / 1024
	return &HostTelemetry{
		Cores:    cores,
		Ram:      PhysicalMemoryAllocation{SoberanaMib: soberanaMib, ReservadoMib: usadoMib, DisponibleMib: disponible / 1024, TechoTotalMib: total / 1024},
		Gpu:      GpuBlackwellTelemetry{Fuente: "ausente"},
		Fuente:   "procfs",
		MonoMs:   time.Since(t.inicio).Milliseconds(),
		WallTime: time.Now().UTC().Format(time.RFC3339),
	}, nil
}

// Capsule sampling has its own lock: the host's initial sleep must never block /state.
// wallNs and ts use the caller's epoch; identidad prevents deltas across PID reuse or cgroup moves.
type muestraCap struct {
	cpuNs, wallNs, ts int64
	source, identidad string
}

func (t *telemetria) Capsula(pid int, epoch time.Time) StateTelemetry {
	t.muCap.Lock()
	defer t.muCap.Unlock()
	if pid <= 0 {
		return StateTelemetry{}
	}
	cpu, rss, source, identidad, ok := leerCapsula(pid, "/proc", "/sys/fs/cgroup")
	if !ok {
		delete(t.prevCap, pid)
		return StateTelemetry{}
	}
	elapsed := time.Since(epoch)
	sample := muestraCap{cpuNs: cpu, wallNs: elapsed.Nanoseconds(), ts: max(int64(1), elapsed.Milliseconds()), source: source, identidad: identidad}
	return t.registrarCapsula(pid, sample, rss)
}

// Caller owns muCap. A gauge does not need a previous sample, unlike CPU load.
func (t *telemetria) registrarCapsula(pid int, sample muestraCap, rss int64) StateTelemetry {
	if t.prevCap == nil {
		t.prevCap = make(map[int]muestraCap)
	}
	previous, exists := t.prevCap[pid]
	if !exists && len(t.prevCap) >= 64 {
		oldest := 0
		for key, value := range t.prevCap {
			if oldest == 0 || value.ts < t.prevCap[oldest].ts || (value.ts == t.prevCap[oldest].ts && key < oldest) {
				oldest = key
			}
		}
		delete(t.prevCap, oldest)
	}
	t.prevCap[pid] = sample
	result := StateTelemetry{}
	if rss > 0 {
		result.Memory = &StateMetric{Source: sample.source, Timestamp: sample.ts, Value: rss}
	}
	if exists && sample.source == previous.source && sample.identidad == previous.identidad && sample.wallNs > previous.wallNs {
		delta := max(int64(0), sample.cpuNs-previous.cpuNs)
		// Integer multiply/divide without overflow; no affinity-based upper clamp.
		hi, lo := bits.Mul64(uint64(delta), 1000)
		dt := uint64(sample.wallNs - previous.wallNs)
		value := int64(math.MaxInt64)
		if hi < dt {
			quotient, _ := bits.Div64(hi, lo, dt)
			if quotient <= math.MaxInt64 {
				value = int64(quotient)
			}
		}
		// A reset invalidates this delta rather than reporting a plausible zero.
		if sample.cpuNs >= previous.cpuNs {
			result.CPU = &StateMetric{Source: sample.source, Timestamp: sample.ts, Value: value}
		}
	}
	return result
}

// Kernel procfs user/system counters have USER_HZ=100 on this Linux target.
// Paths are kernel data. Never attribute the cockpit's shared cgroup to an individual slot.
func leerCapsula(pid int, procRoot, cgroupRoot string) (cpu, rss int64, source, identidad string, ok bool) {
	base := filepath.Join(procRoot, strconv.Itoa(pid))
	stat, err := os.ReadFile(filepath.Join(base, "stat"))
	if err != nil {
		return
	}
	end := strings.LastIndexByte(string(stat), ')')
	if end < 0 {
		return
	}
	fields := strings.Fields(string(stat)[end+1:])
	if len(fields) < 20 || fields[0] == "Z" || fields[0] == "X" {
		return
	}
	user, err := strconv.ParseInt(fields[11], 10, 64)
	if err != nil || user < 0 {
		return
	}
	system, err := strconv.ParseInt(fields[12], 10, 64)
	if err != nil || system < 0 || user > math.MaxInt64-system || user+system > math.MaxInt64/10_000_000 {
		return
	}
	cpu = (user + system) * 10_000_000
	identidad = fields[19]
	source = "procfs"
	group := grupoCapsula(filepath.Join(base, "cgroup"))
	self := grupoCapsula(filepath.Join(procRoot, "self", "cgroup"))
	if group != "" && self != "" && group != self {
		dir := filepath.Join(cgroupRoot, strings.TrimPrefix(group, "/"))
		usage, cpuOK := usoCgroup(filepath.Join(dir, "cpu.stat"))
		memory, memOK := enteroCapsula(filepath.Join(dir, "memory.current"))
		if cpuOK && memOK && usage <= math.MaxInt64/1000 {
			return usage * 1000, memory, "cgroup", identidad + ":" + group, true
		}
	}
	statm, err := os.ReadFile(filepath.Join(base, "statm"))
	if err != nil {
		return 0, 0, "", "", false
	}
	memFields := strings.Fields(string(statm))
	if len(memFields) < 2 {
		return 0, 0, "", "", false
	}
	pages, err := strconv.ParseInt(memFields[1], 10, 64)
	if err != nil || pages < 0 || pages > math.MaxInt64/int64(os.Getpagesize()) {
		return 0, 0, "", "", false
	}
	return cpu, pages * int64(os.Getpagesize()), source, identidad, true
}

func grupoCapsula(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		if group, found := strings.CutPrefix(line, "0::"); found && strings.HasPrefix(group, "/") && filepath.Clean(group) == group {
			return group
		}
	}
	return ""
}

func enteroCapsula(path string) (int64, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	value, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
	return value, err == nil && value >= 0
}

func usoCgroup(path string) (int64, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[0] == "usage_usec" {
			value, err := strconv.ParseInt(fields[1], 10, 64)
			return value, err == nil && value >= 0
		}
	}
	return 0, false
}
