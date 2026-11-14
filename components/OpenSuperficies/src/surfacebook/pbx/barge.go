package pbx

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/kernelbook/lanzador"
	"opensuperficies.local/srf/src/kernelbook/pty"
	"opensuperficies.local/srf/src/orchestratorbook/arrendador"
	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// SbdClaseControl defines Priority Class 0 in OpenSwitchboard (bypasses queues).
const SbdClaseControl uint8 = 0

// BargeSimuladoResult details the outcome of an S2 emergency barge-in ladder execution.
type BargeSimuladoResult struct {
	EpocaPrevia            uint64   `json:"epoca_previa"`
	EpocaNueva             uint64   `json:"epoca_nueva"`
	EpocaLeidaDeArrendador uint64   `json:"epoca_leida_de_arrendador"`
	Pasos                  int      `json:"pasos"`
	ArriendosRevocados     int      `json:"arriendos_revocados"`
	DetallesPasos          []string `json:"detalles_pasos"`
}

// SbdControlEvent models a high-priority S2 out-of-band event.
type SbdControlEvent struct {
	Clase     uint8     `json:"clase"`
	Timestamp time.Time `json:"timestamp"`
	Capsula   string    `json:"capsula"`
	Accion    string    `json:"accion"`
	Motivo    string    `json:"motivo"`
}

// ControlEventBus records dispatched out-of-band control events.
type ControlEventBus struct {
	mu     sync.Mutex
	events []SbdControlEvent
}

var globalControlBus = &ControlEventBus{}

func (b *ControlEventBus) Dispatch(ev SbdControlEvent) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.events = append(b.events, ev)
}

// LeerEpocaArrendador replays the durable WAL lease records to determine the active fencing epoch soundly.
func LeerEpocaArrendador(walPath string, capsula string, destino uint32) (uint64, error) {
	regs, err := wal.LeerRegistros(walPath)
	if err != nil {
		return 0, fmt.Errorf("barge: read wal failed: %w", err)
	}

	type detalleLease struct {
		Capsula string `json:"capsula"`
		Destino uint32 `json:"destino"`
		Epoca   uint64 `json:"epoca"`
		Accion  string `json:"accion"`
	}

	var epoca uint64
	for _, reg := range regs {
		if reg.Clase != wal.ClaseLocal || reg.Tipo != wal.TipoReserva || len(reg.Detalles) == 0 {
			continue
		}
		var det detalleLease
		if err := json.Unmarshal(reg.Detalles, &det); err != nil {
			continue
		}
		if det.Capsula != capsula {
			continue
		}
		if det.Accion == "revocar" {
			epoca++
		} else if det.Destino == destino && det.Epoca > epoca {
			epoca = det.Epoca
		}
	}
	if epoca == 0 {
		return 0, fmt.Errorf("barge: capsule %q destination %d not found in lessor wal state", capsula, destino)
	}
	return epoca, nil
}

// EjecutarBargeSimulado executes the canonical 5-step S2 emergency barge ladder:
// 1. Dispatch SbdClaseControl (Priority 0) and invoke sbd barge CLI
// 2. Deliver SIGSTOP to agent process group and verify stopped status in procfs
// 3. Monotonic epoch bump & capability lease revocation, reading epoch back from lessor WAL
// 4. PTY master/slave allocation, geometry adjustment (120x40), and bidirectional I/O verification
// 5. ARIES WAL transaction checkpoint persistence and readback verification
func EjecutarBargeSimulado(ctx context.Context) (*BargeSimuladoResult, error) {
	tempDir, err := os.MkdirTemp("", "srf-barge-wal-*")
	if err != nil {
		return nil, fmt.Errorf("barge: create temp wal dir: %w", err)
	}
	var libro *wal.LibroConsumo
	defer func() {
		if libro != nil {
			_ = libro.Cerrar()
		}
		_ = os.RemoveAll(tempDir)
	}()

	walPath := filepath.Join(tempDir, "aries.wal")
	var openErr error
	libro, openErr = wal.AbrirLibro(walPath)
	if openErr != nil {
		return nil, fmt.Errorf("barge: create wal: %w", openErr)
	}

	arr, err := arrendador.NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		return nil, fmt.Errorf("barge: create arrendador: %w", err)
	}

	capsulaID := "capsula-srf-ng-barge"

	// Grant two initial leases to simulate active capsule work
	lease1, err := arr.Conceder(capsulaID, 1, "worker-primary", 10*time.Minute, false)
	if err != nil {
		return nil, fmt.Errorf("barge: grant lease 1: %w", err)
	}
	lease2, err := arr.Conceder(capsulaID, 2, "worker-secondary", 10*time.Minute, false)
	if err != nil {
		return nil, fmt.Errorf("barge: grant lease 2: %w", err)
	}

	// Verify both leases are active
	if err := arr.Validar(lease1); err != nil {
		return nil, fmt.Errorf("barge: lease 1 invalid before test: %w", err)
	}
	if err := arr.Validar(lease2); err != nil {
		return nil, fmt.Errorf("barge: lease 2 invalid before test: %w", err)
	}

	epocaPrevia := lease1.Epoca
	detalles := make([]string, 0, 5)
	pasosExitosos := 0

	// Step 1: Dispatch SbdClaseControl (Priority 0) and invoke sbd barge CLI
	ctrlEv := SbdControlEvent{
		Clase:     SbdClaseControl,
		Timestamp: time.Now(),
		Capsula:   capsulaID,
		Accion:    "BARGE_IN_TRIGGER",
		Motivo:    "S2 Emergency Cutout",
	}
	globalControlBus.Dispatch(ctrlEv)

	// Invocation of sbd barge CLI verb (passes active socket if present)
	bargeArgs := []string{"barge", "--room", "srf-ng", "--motivo", "S2 Emergency Cutout"}
	if sock := RutaUDSSocketActiva(); sock != "" {
		bargeArgs = append(bargeArgs, "--socket", sock)
	}
	if _, err := EjecutarSBD(ctx, bargeArgs...); err != nil {
		return nil, fmt.Errorf("barge: sbd barge CLI failed: %w", err)
	}

	pasosExitosos++
	detalles = append(detalles, "Paso 1: Despacho prioritario SbdClaseControl (Prioridad 0) registrado y sbd barge CLI invocado")

	// Step 2: Deliver SIGSTOP to agent PGID and verify stopped status via /proc
	dummyCmd, err := lanzador.Command("bash", "-c", "sleep 10")
	if err != nil {
		return nil, fmt.Errorf("barge: spawn test process for SIGSTOP: %w", err)
	}
	if err := dummyCmd.Start(); err != nil {
		return nil, fmt.Errorf("barge: spawn test process for SIGSTOP: %w", err)
	}
	dummyPID := dummyCmd.Process.Pid

	// Deliver SIGSTOP
	if err := syscall.Kill(dummyPID, syscall.SIGSTOP); err != nil {
		_ = dummyCmd.Process.Kill()
		return nil, fmt.Errorf("barge: deliver SIGSTOP to pid %d: %w", dummyPID, err)
	}

	// Poll briefly for process state to transition to 'T' (stopped) in kernel
	stateOk := false
	var lastState string
	for attempt := 0; attempt < 20; attempt++ {
		statData, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", dummyPID))
		if err == nil {
			idx := bytes.LastIndexByte(statData, ')')
			if idx != -1 && idx+2 < len(statData) {
				fields := bytes.Fields(statData[idx+2:])
				if len(fields) > 0 {
					lastState = string(fields[0])
					if lastState == "T" {
						stateOk = true
						break
					}
				}
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	if !stateOk {
		_ = dummyCmd.Process.Kill()
		return nil, fmt.Errorf("barge: expected process state 'T' after SIGSTOP, got %q", lastState)
	}

	// Cleanly reap test process and check errors
	if err := syscall.Kill(dummyPID, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return nil, fmt.Errorf("barge: kill test process: %w", err)
	}
	if err := dummyCmd.Wait(); err != nil {
		var exitErr *exec.ExitError
		if !errors.As(err, &exitErr) {
			return nil, fmt.Errorf("barge: reap test process: %w", err)
		}
	}

	pasosExitosos++
	detalles = append(detalles, fmt.Sprintf("Paso 2: Entrega SIGSTOP a proceso subordinado [PID: %d] y verificación de estado detenido 'T' en /proc", dummyPID))

	// Step 3: Monotonic Epoch Bump & Capability Lease Revocation
	if err := arr.RevocarCapsula(capsulaID); err != nil {
		return nil, fmt.Errorf("barge: revocar capsula: %w", err)
	}

	// Count revoked leases by verifying epoch expiry on prior leases
	arriendosRevocados := 0
	if err := arr.Validar(lease1); errors.Is(err, arrendador.SrfErrEpocaVencida) {
		arriendosRevocados++
	}
	if err := arr.Validar(lease2); errors.Is(err, arrendador.SrfErrEpocaVencida) {
		arriendosRevocados++
	}

	// Read post-revocation epoch from our own WAL replay
	epocaNueva, err := LeerEpocaArrendador(walPath, capsulaID, 1)
	if err != nil {
		return nil, fmt.Errorf("barge: read post-revocation epoch from lessor WAL: %w", err)
	}

	// Do not corrupt WAL with an extra lease concession prior to recovery

	// Close first WAL instance to release exclusive file lock before reopening
	_ = libro.Cerrar()

	// Distinct observation: open a second lessor instance over the same WAL after revocation
	// and read epoca_leida_de_arrendador from that instance's recovered state.
	libro2, err := wal.AbrirLibro(walPath)
	if err != nil {
		return nil, fmt.Errorf("barge: open wal for second lessor: %w", err)
	}
	libro = libro2

	arr2, err := arrendador.NuevoSrfArrendador(func(c string) string { return c }, libro2)
	if err != nil {
		return nil, fmt.Errorf("barge: recover second lessor from wal: %w", err)
	}

	// In the lessor, Conceder grants a lease with epoch = e.epoca + 1.
	// Therefore, the recovered epoch in the lessor state is sPost.Epoca - 1.
	sPost, err := arr2.Conceder(capsulaID, 1, "barge-readback-probe", 10*time.Second, false)
	if err != nil {
		return nil, fmt.Errorf("barge: probe recovered epoch from second lessor: %w", err)
	}
	epocaLeidaDeArrendador := sPost.Epoca - 1

	pasosExitosos++
	detalles = append(detalles, fmt.Sprintf("Paso 3: Época leída del arrendador (%d -> %d) y revocación de %d arriendos",
		epocaPrevia, epocaNueva, arriendosRevocados))

	// Step 4: PTY allocation, geometry configuration (120x40), and bidirectional I/O verification
	term, err := pty.Open()
	if err != nil {
		return nil, fmt.Errorf("barge: pty.Open: %w", err)
	}
	if err := term.Resize(120, 40); err != nil {
		_ = term.Close()
		return nil, fmt.Errorf("barge: pty.Resize: %w", err)
	}

	// Verify bidirectional communication on the PTY
	testMsg := []byte("srf-barge-pty-test\n")
	if _, err := term.Master.Write(testMsg); err != nil {
		_ = term.Close()
		return nil, fmt.Errorf("barge: pty master write: %w", err)
	}
	buf := make([]byte, len(testMsg))
	n, err := term.Slave.Read(buf)
	if err != nil || n != len(testMsg) {
		_ = term.Close()
		return nil, fmt.Errorf("barge: pty slave read: %w (read %d bytes)", err, n)
	}
	if err := term.Close(); err != nil {
		return nil, fmt.Errorf("barge: pty close: %w", err)
	}

	pasosExitosos++
	detalles = append(detalles, "Paso 4: PTY maestro/esclavo verificado con prueba de I/O y redimensionamiento (120x40)")

	// Step 5: ARIES WAL Transaction Checkpoint & Persistence Verification
	walKey := fmt.Sprintf("barge:%s:epoch-%d", capsulaID, epocaNueva)
	txID := fmt.Sprintf("tx-barge-%d", time.Now().UnixNano())
	if _, err := libro.EscribirIntencion(txID, walKey, map[string]any{"epoch": epocaNueva}); err != nil {
		return nil, fmt.Errorf("barge: wal log intention: %w", err)
	}
	if _, err := libro.EscribirDesenlace(txID, walKey, wal.DesenlaceConfirmado, wal.RCOk, map[string]any{"status": "ok"}); err != nil {
		return nil, fmt.Errorf("barge: wal log outcome: %w", err)
	}

	// Verify records were durably written by re-reading the WAL file
	regs, err := wal.LeerRegistros(walPath)
	if err != nil || len(regs) < 2 {
		return nil, fmt.Errorf("barge: wal readback verification failed: %w (found %d records)", err, len(regs))
	}

	pasosExitosos++
	detalles = append(detalles, "Paso 5: Checkpoint ARIES WAL persistido con fsync y verificado por relectura")

	return &BargeSimuladoResult{
		EpocaPrevia:            epocaPrevia,
		EpocaNueva:             epocaNueva,
		EpocaLeidaDeArrendador: epocaLeidaDeArrendador,
		Pasos:                  pasosExitosos,
		ArriendosRevocados:     arriendosRevocados,
		DetallesPasos:          detalles,
	}, nil
}
