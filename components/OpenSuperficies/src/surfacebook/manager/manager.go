package manager

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"opensuperficies.local/srf/src/displaybook/puertos"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
	"opensuperficies.local/srf/src/orchestratorbook/arrendador"
	"opensuperficies.local/srf/src/orchestratorbook/gobernador"
	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

var (
	// ErrSlotOcupado is returned when the slot is already mediated or is in the middle of a transition.
	ErrSlotOcupado = errors.New("manager: slot already active or transitioning")
	// ErrSlotAusente is returned when a stop names a slot this daemon does not mediate and no class.
	ErrSlotAusente = errors.New("manager: slot is not mediated by this daemon")
	// ErrClaseInvalida is returned for a class outside the closed set of capsule classes.
	ErrClaseInvalida = errors.New("manager: unknown capsule class")
	// ErrClaseDistinta is returned when a stop names a class different from the one the slot was started with.
	ErrClaseDistinta = errors.New("manager: capsule class does not match the mediated slot")
)

// ClaseDefecto is the class used when a start request names none.
const ClaseDefecto = "browser"

// clasesValidas is the closed set of capsule classes the dispatcher bin/srf-capsula routes.
var clasesValidas = map[string]bool{"browser": true, "desktop": true, "microvm": true}

func claseValida(c string) bool { return clasesValidas[c] }

// Lifecycle states of a mediated slot.
const (
	EstadoIniciando  = "iniciando"
	EstadoListo      = "listo"
	EstadoDeteniendo = "deteniendo"
	// EstadoHuerfana marks a slot whose capsule could not be confirmed stopped after a failed start:
	// it stays in the table, keeps its reservation and lease, and a later stop releases them.
	EstadoHuerfana = "huerfana"
)

// SurfaceInfo records the live orchestration metadata for a mediated surface.
type SurfaceInfo struct {
	Slot       uint16  `json:"slot"`
	Clase      string  `json:"clase"`
	Contenedor string  `json:"contenedor,omitempty"`
	ID         string  `json:"id,omitempty"`
	PuertoXpra uint16  `json:"puerto_xpra,omitempty"`
	PuertoCDP  uint16  `json:"puerto_cdp,omitempty"`
	LeaseEpoca uint64  `json:"lease_epoca,omitempty"`
	PID        int     `json:"pid,omitempty"`
	Reserva    uint64  `json:"reserva,omitempty"`
	CPUs       float64 `json:"cpus,omitempty"`
	Estado     string  `json:"estado"`
}

// SrfEstado is the compact operational state (token ceiling <= 120). Its six fields are fixed.
type SrfEstado struct {
	Estado        string `json:"estado"`
	Parada        string `json:"parada"`
	Vivas         int    `json:"vivas"`
	ReservadoMiB  uint64 `json:"reservado_mib"`
	DisponibleMiB uint64 `json:"disponible_mib"`
	SoberanaMiB   uint64 `json:"soberana_mib"`
}

// OpcionesManager holds what differs between production and a test; the zero value is production.
type OpcionesManager struct {
	CapsulaBin string        // dispatcher executable; "" => <repo>/bin/srf-capsula
	RepoDir    string        // working directory of every dispatcher call; "" => the repository root
	Timeout    time.Duration // per dispatcher call; 0 => 120 s
	Censo      CensoOpciones
}

// SurfaceManager orchestrates the L6 surface lifecycle: every start and stop of a capsule
// passes through it, after the stop signal, the governor and the lessor.
type SurfaceManager struct {
	gov     *gobernador.SrfGobernador
	arr     *arrendador.SrfArrendador
	guardia Guardia
	wal     *wal.LibroConsumo

	capsulaBin string
	repoDir    string
	timeout    time.Duration
	censo      CensoOpciones

	mu       sync.RWMutex
	capsulas map[uint16]*SurfaceInfo
}

// NuevoSurfaceManager binds governor, lessor, stop signal and journal. All four are required.
func NuevoSurfaceManager(gov *gobernador.SrfGobernador, arr *arrendador.SrfArrendador, guardia Guardia, libro *wal.LibroConsumo, opts OpcionesManager) (*SurfaceManager, error) {
	switch {
	case gov == nil:
		return nil, errors.New("manager: governor is required")
	case arr == nil:
		return nil, errors.New("manager: lessor is required")
	case guardia == nil:
		return nil, errors.New("manager: stop-signal guard is required")
	case libro == nil:
		return nil, errors.New("manager: journal is required")
	}
	if opts.RepoDir == "" {
		opts.RepoDir = findRepoRoot()
	}
	if opts.CapsulaBin == "" {
		opts.CapsulaBin = filepath.Join(opts.RepoDir, "bin", "srf-capsula")
	}
	if opts.Timeout <= 0 {
		opts.Timeout = 120 * time.Second
	}
	if opts.Censo.RepoDir == "" {
		opts.Censo.RepoDir = opts.RepoDir
	}
	return &SurfaceManager{
		gov:        gov,
		arr:        arr,
		guardia:    guardia,
		wal:        libro,
		capsulaBin: opts.CapsulaBin,
		repoDir:    opts.RepoDir,
		timeout:    opts.Timeout,
		censo:      opts.Censo,
		capsulas:   make(map[uint16]*SurfaceInfo),
	}, nil
}

func etiquetaCapsula(slot uint16) string { return fmt.Sprintf("srf-ng-%d", slot) }

// ejecutarCapsula runs one verb of the dispatcher through the allowlisted launcher.
// The launcher's allowlist names bash but not bin/srf-capsula (only bin/srf-capsula-*), so the
// dispatcher is reached through a CONSTANT bash script that receives the executable and every
// argument as positional parameters: no request field is ever interpolated into shell text.
// The class is always named explicitly: an omitted class makes the dispatcher assume "browser".
func (m *SurfaceManager) ejecutarCapsula(ctx context.Context, accion, clase string, slot uint16) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, m.timeout)
	defer cancel()

	cmd, err := lanzador.CommandContext(ctx, "bash", "-c", `exec "$0" "$@"`,
		m.capsulaBin, accion, "--clase", clase, "--slot", strconv.Itoa(int(slot)), "--json")
	if err != nil {
		return nil, fmt.Errorf("launcher: %w", err)
	}
	cmd.Dir = m.repoDir
	cmd.WaitDelay = 3 * time.Second

	out, err := cmd.Output()
	if err != nil {
		detalle := ""
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			detalle = strings.TrimSpace(string(ee.Stderr))
			if len(detalle) > 300 {
				detalle = detalle[:300]
			}
		}
		if ctx.Err() != nil {
			return nil, fmt.Errorf("%s slot %d: %w (%v)", accion, slot, err, ctx.Err())
		}
		if detalle != "" {
			return nil, fmt.Errorf("%s slot %d: %w: %s", accion, slot, err, detalle)
		}
		return nil, fmt.Errorf("%s slot %d: %w", accion, slot, err)
	}
	return out, nil
}

// IniciarCapsula mediates a start: stop signal, slot reservation, governor admission, lease,
// the stop signal AGAIN immediately before the dispatch, then the class dispatcher.
// Every step it took is undone on failure and every error of an undo is reported, never dropped.
func (m *SurfaceManager) IniciarCapsula(ctx context.Context, clase string, slot uint16) (info *SurfaceInfo, err error) {
	if clase == "" {
		clase = ClaseDefecto
	}

	// 1. The stop signal comes first: a refused request must not touch any state.
	if err := m.guardia.ExigirLibre(); err != nil {
		return nil, err
	}
	if !claseValida(clase) {
		return nil, fmt.Errorf("%w: %q", ErrClaseInvalida, clase)
	}

	// 2. Reserve the slot before anything slow happens, so two starts cannot both pass.
	m.mu.Lock()
	if _, ocupado := m.capsulas[slot]; ocupado {
		m.mu.Unlock()
		return nil, ErrSlotOcupado
	}
	m.capsulas[slot] = &SurfaceInfo{Slot: slot, Clase: clase, Estado: EstadoIniciando}
	m.mu.Unlock()

	var (
		dec       gobernador.SrfDecisionAdmision
		admitida  bool
		arrendada bool
		lanzada   bool
		tag       = etiquetaCapsula(slot)
	)
	defer func() {
		if err == nil {
			return
		}
		info = nil
		if lanzada {
			// The capsule started but its answer was unusable: it exists and nobody would track it.
			if _, derr := m.ejecutarCapsula(context.Background(), "detener", clase, slot); derr != nil {
				err = fmt.Errorf("%w; rollback: capsule teardown failed, slot kept as %s: %w", err, EstadoHuerfana, derr)
				m.mu.Lock()
				m.capsulas[slot] = &SurfaceInfo{Slot: slot, Clase: clase, Estado: EstadoHuerfana, Reserva: dec.Reserva, CPUs: dec.CPUs}
				m.mu.Unlock()
				return
			}
		}
		if arrendada {
			if rerr := m.arr.RevocarCapsula(tag); rerr != nil {
				err = fmt.Errorf("%w; rollback: lease revocation for %s not journaled: %w", err, tag, rerr)
			}
		}
		if admitida {
			m.gov.Devolver(dec, clase)
		}
		m.mu.Lock()
		delete(m.capsulas, slot)
		m.mu.Unlock()
	}()

	// 3. Governor admission arithmetic.
	dec = m.gov.Admitir(gobernador.SrfSolicitudAdmision{
		Clase:     clase,
		Reserva:   gobernador.EstimarReservaBytes(clase, 6),
		CPUs:      1.5,
		CandadoOK: true,
	})
	if !dec.Admitida {
		return nil, fmt.Errorf("gobernador: admission denied: %s", dec.Motivo)
	}
	admitida = true

	// 4. Lessor: a monotonic epoch and a random token, journalled before it is returned.
	lease, lerr := m.arr.Conceder(tag, uint32(slot), "srfd", time.Hour, false)
	if lerr != nil {
		return nil, fmt.Errorf("arrendador: lease denied: %w", lerr)
	}
	arrendada = true

	// 5. The guard is read again immediately before the dispatch it protects (a signal written
	// while admission and lease were being processed must still stop this start).
	if gerr := m.guardia.ExigirLibre(); gerr != nil {
		return nil, gerr
	}

	// 6. Dispatch through bin/srf-capsula.
	out, xerr := m.ejecutarCapsula(ctx, "iniciar", clase, slot)
	if xerr != nil {
		return nil, xerr
	}
	lanzada = true // from here on a capsule exists

	var raw struct {
		Slot       uint16 `json:"slot"`
		Clase      string `json:"clase"`
		Contenedor string `json:"contenedor"`
		ID         string `json:"id"`
		PuertoXpra uint16 `json:"puerto_xpra"`
		PuertoCDP  uint16 `json:"puerto_cdp"`
		PID        int    `json:"pid"`
	}
	if uerr := json.Unmarshal(out, &raw); uerr != nil {
		return nil, fmt.Errorf("srf-capsula iniciar: unusable answer: %w", uerr)
	}
	if raw.Slot != slot {
		return nil, fmt.Errorf("srf-capsula iniciar: answered for slot %d, asked for slot %d", raw.Slot, slot)
	}

	listo := SurfaceInfo{
		Slot:       slot,
		Clase:      clase,
		Contenedor: raw.Contenedor,
		ID:         raw.ID,
		PuertoXpra: raw.PuertoXpra,
		PuertoCDP:  raw.PuertoCDP,
		LeaseEpoca: lease.Epoca,
		PID:        raw.PID,
		Reserva:    dec.Reserva,
		CPUs:       dec.CPUs,
		Estado:     EstadoListo,
	}
	m.mu.Lock()
	copia := listo
	m.capsulas[slot] = &copia
	m.mu.Unlock()
	return &listo, nil
}

// DetenerCapsula mediates a stop. The class is ALWAYS passed to the dispatcher explicitly: it
// is taken from the mediated slot, or from the request when the slot is not mediated. A request
// naming neither is refused instead of letting the dispatcher assume "browser".
func (m *SurfaceManager) DetenerCapsula(ctx context.Context, slot uint16, clase string) error {
	if err := m.guardia.ExigirLibre(); err != nil {
		return err
	}
	if clase != "" && !claseValida(clase) {
		return fmt.Errorf("%w: %q", ErrClaseInvalida, clase)
	}

	m.mu.Lock()
	entrada, mediada := m.capsulas[slot]
	var previa SurfaceInfo
	if mediada {
		if entrada.Estado == EstadoIniciando || entrada.Estado == EstadoDeteniendo {
			m.mu.Unlock()
			return fmt.Errorf("%w: slot %d is %s", ErrSlotOcupado, slot, entrada.Estado)
		}
		if clase != "" && clase != entrada.Clase {
			m.mu.Unlock()
			return fmt.Errorf("%w: slot %d runs class %s, not %s", ErrClaseDistinta, slot, entrada.Clase, clase)
		}
		clase = entrada.Clase
		previa = *entrada
		entrada.Estado = EstadoDeteniendo
	}
	m.mu.Unlock()

	if !mediada && clase == "" {
		return fmt.Errorf("%w: slot %d; name the class explicitly", ErrSlotAusente, slot)
	}

	// The guard is read again immediately before the dispatch it protects: a PARADA written after the
	// first read (or while the slot was being marked) still stops this stop, and the slot goes back.
	err := m.guardia.ExigirLibre()
	if err == nil {
		_, err = m.ejecutarCapsula(ctx, "detener", clase, slot)
	}
	if err != nil {
		if mediada {
			m.mu.Lock()
			m.capsulas[slot] = &previa // the capsule may still exist: keep tracking it
			m.mu.Unlock()
		}
		return err
	}
	if !mediada {
		return nil // nothing of ours to release
	}

	// The capsule is stopped: release what the start took, and report a lease that could not be journalled.
	m.gov.Liberar(previa.Reserva, previa.CPUs, previa.Clase)
	if rerr := m.arr.RevocarCapsula(etiquetaCapsula(slot)); rerr != nil {
		err = fmt.Errorf("capsule stopped but lease revocation for %s not journalled: %w", etiquetaCapsula(slot), rerr)
	}
	m.mu.Lock()
	delete(m.capsulas, slot)
	m.mu.Unlock()
	return err
}

// DetenerTodas stops every mediated capsule through the same guarded path as any client stop
// (so an effective PARADA refuses it too) and returns the slots that are still in the table afterwards.
func (m *SurfaceManager) DetenerTodas(ctx context.Context) ([]uint16, error) {
	type par struct {
		slot  uint16
		clase string
	}
	m.mu.RLock()
	pendientes := make([]par, 0, len(m.capsulas))
	for s, c := range m.capsulas {
		pendientes = append(pendientes, par{s, c.Clase})
	}
	m.mu.RUnlock()
	sort.Slice(pendientes, func(i, j int) bool { return pendientes[i].slot < pendientes[j].slot })

	var err error
	for _, p := range pendientes {
		if derr := m.DetenerCapsula(ctx, p.slot, p.clase); derr != nil {
			err = errors.Join(err, fmt.Errorf("slot %d: %w", p.slot, derr))
			if errors.Is(derr, ErrParadaActiva) {
				break // every remaining stop is refused for the same reason
			}
		}
	}

	m.mu.RLock()
	restantes := make([]uint16, 0, len(m.capsulas))
	for s := range m.capsulas {
		restantes = append(restantes, s)
	}
	m.mu.RUnlock()
	sort.Slice(restantes, func(i, j int) bool { return restantes[i] < restantes[j] })
	return restantes, err
}

// Estado returns the compact operational state of the daemon.
func (m *SurfaceManager) Estado() (*SrfEstado, error) {
	govEst, err := m.gov.Estado()
	if err != nil {
		return nil, fmt.Errorf("manager: governor state: %w", err)
	}
	parada, _ := m.guardia.Estado()
	return &SrfEstado{
		Estado:        "ok", // constant: the state is only produced when the governor telemetry was read
		Parada:        parada,
		Vivas:         govEst.Vivas,
		ReservadoMiB:  govEst.ReservadoBytes / (1024 * 1024),
		DisponibleMiB: govEst.MemDisponibleMiB,
		SoberanaMiB:   govEst.ReservaSoberana / (1024 * 1024),
	}, nil
}

// Listar returns a snapshot of the mediated slots ordered by slot.
func (m *SurfaceManager) Listar() []*SurfaceInfo {
	m.mu.RLock()
	defer m.mu.RUnlock()

	res := make([]*SurfaceInfo, 0, len(m.capsulas))
	for _, c := range m.capsulas {
		copia := *c
		res = append(res, &copia)
	}
	sort.Slice(res, func(i, j int) bool { return res[i].Slot < res[j].Slot })
	return res
}

// conjuntoMediado snapshots what this manager claims to mediate, for the census.
func (m *SurfaceManager) conjuntoMediado() conjuntoMediado {
	m.mu.RLock()
	defer m.mu.RUnlock()

	c := nuevoConjuntoMediado()
	for slot, s := range m.capsulas {
		c.total++
		if s.Contenedor != "" {
			c.contenedores[strings.TrimPrefix(s.Contenedor, "/")] = true
		}
		if s.PuertoXpra > 0 {
			c.puertos[int(s.PuertoXpra)] = true
		}
		if s.PuertoCDP > 0 {
			c.puertos[int(s.PuertoCDP)] = true
		}
		if s.PID > 0 {
			c.pids[s.PID] = true
		}
		if s.Estado == EstadoIniciando || s.Estado == EstadoHuerfana {
			// The dispatcher has not reported (or never will) what it created: the name and ports
			// are the ones the campaign plan assigns to this slot.
			c.contenedores[etiquetaCapsula(slot)] = true
			if par, err := puertos.NuevoPlanPuertos().Par(slot); err == nil {
				c.puertos[int(par.Xpra)] = true
				c.puertos[int(par.CDP)] = true
			}
		}
	}
	return c
}

// Cerrar fsyncs and CLOSES the journal (wal.LibroConsumo.Cerrar syncs, then closes the file and releases
// the flock). It never unlinks the journal or its flock file: they are durable state (the lessor rebuilds
// its fencing epochs from this journal at the next start, and INV-8 / INV-9 inspect it), not residue.
// A failed fsync or close is returned, never swallowed.
func (m *SurfaceManager) Cerrar() error { return cerrarLibro(m.wal) }

func cerrarLibro(libro *wal.LibroConsumo) error {
	if libro == nil {
		return nil
	}
	return libro.Cerrar()
}
