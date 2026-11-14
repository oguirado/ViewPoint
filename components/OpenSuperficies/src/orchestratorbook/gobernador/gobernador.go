package gobernador

import (
	"bufio"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

var mutanteSinGobernador = false

// SrfSolicitudAdmision encapsulates a capsule admission request (OrchestratorBook L5).
type SrfSolicitudAdmision struct {
	Clase     string  `json:"clase"`      // "browser" | "desktop" | "microvm" | "terminal"
	Reserva   uint64  `json:"reserva"`    // bytes = memory.max
	CPUs      float64 `json:"cpus"`       // requested CPU cores
	CandadoOK bool    `json:"candado_ok"` // caller acquired identity flock (or no identity required)
}

// SrfDecisionAdmision represents the outcome of an admission request.
type SrfDecisionAdmision struct {
	Admitida bool    `json:"admitida"`
	Motivo   string  `json:"motivo,omitempty"` // "ram" | "cpu" | "latencia" | "candado" | "techo" | "techo_microvm" | "telemetria" | ""
	Reserva  uint64  `json:"reserva"`
	CPUs     float64 `json:"cpus"`
}

// SrfTelemetria provides host resource telemetry.
type SrfTelemetria interface {
	MemDisponible() (uint64, error)
	PresionCPU10() (float64, error)
	UsoCapsulas() (uint64, error)
	OrigenMem() string
}

// SrfTelemetriaProc reads live host metrics from /proc/meminfo, /proc/pressure/cpu, and cgroup v2.
type SrfTelemetriaProc struct {
	CgroupsCapsulas func() []string
}

// NuevaTelemetriaProc creates a live proc telemetry reader.
func NuevaTelemetriaProc(cgroupsCapsulas func() []string) *SrfTelemetriaProc {
	return &SrfTelemetriaProc{
		CgroupsCapsulas: cgroupsCapsulas,
	}
}

// MemDisponible reads MemAvailable in bytes from /proc/meminfo.
func (t *SrfTelemetriaProc) MemDisponible() (uint64, error) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, fmt.Errorf("gobernador: open /proc/meminfo: %w", err)
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	for sc.Scan() {
		campos := strings.Fields(sc.Text())
		if len(campos) >= 2 && campos[0] == "MemAvailable:" {
			kib, err := strconv.ParseUint(campos[1], 10, 64)
			if err != nil {
				return 0, fmt.Errorf("gobernador: parse MemAvailable: %w", err)
			}
			return kib << 10, nil
		}
	}
	if err := sc.Err(); err != nil {
		return 0, fmt.Errorf("gobernador: scan /proc/meminfo: %w", err)
	}
	return 0, fmt.Errorf("gobernador: MemAvailable ausente en /proc/meminfo")
}

// PresionCPU10 reads 'some avg10' CPU pressure from /proc/pressure/cpu.
func (t *SrfTelemetriaProc) PresionCPU10() (float64, error) {
	b, err := os.ReadFile("/proc/pressure/cpu")
	if err != nil {
		return 0, fmt.Errorf("gobernador: read /proc/pressure/cpu: %w", err)
	}

	for _, linea := range strings.Split(string(b), "\n") {
		if !strings.HasPrefix(linea, "some ") {
			continue
		}
		for _, campo := range strings.Fields(linea) {
			if v, ok := strings.CutPrefix(campo, "avg10="); ok {
				return strconv.ParseFloat(v, 64)
			}
		}
	}
	return 0, fmt.Errorf("gobernador: PSI cpu sin avg10")
}

// UsoCapsulas aggregates current memory usage from all active capsule cgroups.
func (t *SrfTelemetriaProc) UsoCapsulas() (uint64, error) {
	var total uint64
	if t.CgroupsCapsulas != nil {
		for _, ruta := range t.CgroupsCapsulas() {
			b, err := os.ReadFile(ruta)
			if err != nil {
				return 0, fmt.Errorf("gobernador: read cgroup %s: %w", ruta, err)
			}
			v, err := strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64)
			if err != nil {
				return 0, fmt.Errorf("gobernador: parse cgroup %s: %w", ruta, err)
			}
			total += v
		}
	}
	return total, nil
}

// OrigenMem returns the source of memory telemetry.
func (t *SrfTelemetriaProc) OrigenMem() string {
	return "/proc/meminfo"
}

// TelemetriaSintetica provides a fixed/mock telemetry source for deterministic tests and simulations.
type TelemetriaSintetica struct {
	DisponibleBytes uint64
	PresionCPU      float64
	UsoBytes        uint64
	Origen          string
	ErrMem          error
	ErrCPU          error
	ErrUso          error
}

func (t *TelemetriaSintetica) MemDisponible() (uint64, error) {
	return t.DisponibleBytes, t.ErrMem
}

func (t *TelemetriaSintetica) PresionCPU10() (float64, error) {
	return t.PresionCPU, t.ErrCPU
}

func (t *TelemetriaSintetica) UsoCapsulas() (uint64, error) {
	return t.UsoBytes, t.ErrUso
}

func (t *TelemetriaSintetica) OrigenMem() string {
	if t.Origen != "" {
		return t.Origen
	}
	return "/proc/meminfo"
}

// SrfGobernador is the 4-variable admission governor (Memory Slack, PSI, CDP SLA, Identity Lock).
type SrfGobernador struct {
	mu                sync.Mutex
	Tel               SrfTelemetria
	ReservaSoberana   uint64        // 24 GiB Sovereign Reserve [TARGET]
	UmbralPSI         float64       // 20% PSI some avg10 [TARGET]
	LatSLA            time.Duration // 250 ms CDP SLA [TARGET]
	MaxCapsulas       int           // 24 Hard ceiling [TARGET]
	MicrovmActivasMax int           // 2 Max active microVMs [TARGET]

	reservado    uint64
	cpus         float64
	vivas        int
	microvmVivas int
	lat          [256]time.Duration
	nlat         int
	ilat         int
}

// NuevoGobernador initializes a new governor instance with normative default constants.
func NuevoGobernador(tel SrfTelemetria) *SrfGobernador {
	return &SrfGobernador{
		Tel:               tel,
		ReservaSoberana:   24 * 1024 * 1024 * 1024, // 24 GiB
		UmbralPSI:         20.0,                    // 20%
		LatSLA:            250 * time.Millisecond,  // 250 ms
		MaxCapsulas:       24,                      // N_max = 24
		MicrovmActivasMax: 2,                       // 2 microvms max
	}
}

// Admitir applies the 4-variable admission predicate:
//
//	Admit(J) = (Holgura(t) - B_req >= M_sob) ∧
//	           (PSI_10 < θ_CPU) ∧
//	           (p99_CDP < L_SLA ∨ (n_samples < 32 ∧ |V| < 4)) ∧
//	           (flock_EX|NB == OK) ∧
//	           (|V| < N_max)
func (g *SrfGobernador) Admitir(s SrfSolicitudAdmision) SrfDecisionAdmision {
	g.mu.Lock()
	defer g.mu.Unlock()

	if mutanteSinGobernador {
		// Mutant v1 behavior: blindly admit every request with 4 GiB reservation
		reserva := uint64(4 * 1024 * 1024 * 1024)
		g.reservado += reserva
		g.vivas++
		if s.Clase == "microvm" {
			g.microvmVivas++
		}
		return SrfDecisionAdmision{
			Admitida: true,
			Reserva:  reserva,
			CPUs:     s.CPUs,
		}
	}

	no := func(m string) SrfDecisionAdmision { return SrfDecisionAdmision{Motivo: m} }

	if !s.CandadoOK {
		return no("candado")
	}
	if g.vivas >= g.MaxCapsulas {
		return no("techo")
	}
	if s.Clase == "microvm" && g.microvmVivas >= g.MicrovmActivasMax {
		return no("techo_microvm")
	}

	disp, err1 := g.Tel.MemDisponible()
	uso, err2 := g.Tel.UsoCapsulas()
	psi, err3 := g.Tel.PresionCPU10()
	if err1 != nil || err2 != nil || err3 != nil {
		return no("telemetria") // fail-closed
	}

	crecimiento := int64(g.reservado) - int64(uso)
	if crecimiento < 0 {
		crecimiento = 0
	}
	holgura := int64(disp) - crecimiento - int64(s.Reserva)
	if holgura < int64(g.ReservaSoberana) {
		return no("ram")
	}

	if psi >= g.UmbralPSI {
		return no("cpu")
	}

	if g.nlat < 32 {
		if g.vivas >= 3 {
			return no("latencia")
		}
	} else if g.p99() >= g.LatSLA {
		return no("latencia")
	}

	g.reservado += s.Reserva
	g.cpus += s.CPUs
	g.vivas++
	if s.Clase == "microvm" {
		g.microvmVivas++
	}

	return SrfDecisionAdmision{
		Admitida: true,
		Reserva:  s.Reserva,
		CPUs:     s.CPUs,
	}
}

// Devolver undoes an admission if capsule failed to boot.
func (g *SrfGobernador) Devolver(d SrfDecisionAdmision, clase string) {
	if d.Admitida {
		g.Liberar(d.Reserva, d.CPUs, clase)
	}
}

// Liberar frees resources upon capsule termination.
func (g *SrfGobernador) Liberar(reserva uint64, cpus float64, clase string) {
	g.mu.Lock()
	defer g.mu.Unlock()

	if g.vivas > 0 {
		g.vivas--
	}
	if clase == "microvm" && g.microvmVivas > 0 {
		g.microvmVivas--
	}
	if g.reservado >= reserva {
		g.reservado -= reserva
	} else {
		g.reservado = 0
	}
	if g.cpus >= cpus {
		g.cpus -= cpus
	} else {
		g.cpus = 0
	}
}

// ObservarLatencia records a round-trip CDP latency measurement.
func (g *SrfGobernador) ObservarLatencia(d time.Duration) {
	g.mu.Lock()
	defer g.mu.Unlock()

	g.lat[g.ilat] = d
	g.ilat = (g.ilat + 1) % len(g.lat)
	if g.nlat < len(g.lat) {
		g.nlat++
	}
}

func (g *SrfGobernador) p99() time.Duration {
	if g.nlat == 0 {
		return 0
	}
	v := make([]time.Duration, g.nlat)
	copy(v, g.lat[:g.nlat])
	sort.Slice(v, func(i, j int) bool { return v[i] < v[j] })
	k := (99*len(v) + 99) / 100
	if k > 0 {
		k--
	}
	return v[k]
}

// SrfEstadoGobernador holds the current operational telemetry and state.
type SrfEstadoGobernador struct {
	OrigenMem          string  `json:"origen_mem"`
	MemDisponibleBytes uint64  `json:"mem_disponible_bytes"`
	MemDisponibleMiB   uint64  `json:"mem_disponible_mib"`
	ReservaSoberana    uint64  `json:"reserva_soberana_bytes"`
	MicrovmActivasMax  int     `json:"microvm_activas_max"`
	MaxCapsulas        int     `json:"max_capsulas"`
	Vivas              int     `json:"vivas"`
	ReservadoBytes     uint64  `json:"reservado_bytes"`
	Cpus               float64 `json:"cpus"`
	PresionCPU10       float64 `json:"presion_cpu_avg10"`
}

// Estado returns the live state of the governor.
func (g *SrfGobernador) Estado() (SrfEstadoGobernador, error) {
	g.mu.Lock()
	defer g.mu.Unlock()

	disp, err1 := g.Tel.MemDisponible()
	psi, err2 := g.Tel.PresionCPU10()

	origen := g.Tel.OrigenMem()
	if origen == "" {
		origen = "/proc/meminfo"
	}

	est := SrfEstadoGobernador{
		OrigenMem:          origen,
		MemDisponibleBytes: disp,
		MemDisponibleMiB:   disp / (1024 * 1024),
		ReservaSoberana:    g.ReservaSoberana,
		MicrovmActivasMax:  g.MicrovmActivasMax,
		MaxCapsulas:        g.MaxCapsulas,
		Vivas:              g.vivas,
		ReservadoBytes:     g.reservado,
		Cpus:               g.cpus,
		PresionCPU10:       psi,
	}
	if err1 != nil {
		return est, fmt.Errorf("telemetria mem: %w", err1)
	}
	if err2 != nil {
		return est, fmt.Errorf("telemetria psi: %w", err2)
	}
	return est, nil
}

// ResultadoSimulacion captures the output of an admission simulation.
type ResultadoSimulacion struct {
	Admitidas          int    `json:"admitidas"`
	Denegadas          int    `json:"denegadas"`
	TotalSolicitadas   int    `json:"total_solicitadas"`
	ReservaUnitariaMiB uint64 `json:"reserva_unitaria_mib"`
	MemDisponibleMiB   uint64 `json:"mem_disponible_mib"`
	MemSoberanaMiB     uint64 `json:"mem_soberana_mib"`
	MemComprometidaMiB uint64 `json:"mem_comprometida_mib"`
	HolguraFinalMiB    int64  `json:"holgura_final_mib"`
	Desbordamiento     bool   `json:"desbordamiento"`
}

// Simular runs a simulation of N admissions against fixed available memory.
func Simular(n int, clase string, paginas int, memDisponibleMiB uint64) ResultadoSimulacion {
	tel := &TelemetriaSintetica{
		DisponibleBytes: memDisponibleMiB * 1024 * 1024,
		PresionCPU:      0.0,
		UsoBytes:        0,
		Origen:          "/proc/meminfo",
	}
	gov := NuevoGobernador(tel)
	// Populate healthy latency samples to satisfy SLA check in simulation
	for i := 0; i < 32; i++ {
		gov.ObservarLatencia(50 * time.Millisecond)
	}

	reservaBytes := EstimarReservaBytes(clase, paginas)
	reservaUnitariaMiB := reservaBytes / (1024 * 1024)
	if mutanteSinGobernador {
		reservaUnitariaMiB = 4096
	}

	admitidas := 0
	denegadas := 0

	for i := 0; i < n; i++ {
		sol := SrfSolicitudAdmision{
			Clase:     clase,
			Reserva:   reservaBytes,
			CPUs:      1.5,
			CandadoOK: true,
		}
		dec := gov.Admitir(sol)
		if dec.Admitida {
			admitidas++
		} else {
			denegadas++
		}
	}

	memComprometidaMiB := uint64(admitidas) * reservaUnitariaMiB
	memSoberanaMiB := gov.ReservaSoberana / (1024 * 1024)
	holguraFinalMiB := int64(memDisponibleMiB) - int64(memComprometidaMiB)
	desbordamiento := holguraFinalMiB < int64(memSoberanaMiB)

	return ResultadoSimulacion{
		Admitidas:          admitidas,
		Denegadas:          denegadas,
		TotalSolicitadas:   n,
		ReservaUnitariaMiB: reservaUnitariaMiB,
		MemDisponibleMiB:   memDisponibleMiB,
		MemSoberanaMiB:     memSoberanaMiB,
		MemComprometidaMiB: memComprometidaMiB,
		HolguraFinalMiB:    holguraFinalMiB,
		Desbordamiento:     desbordamiento,
	}
}
