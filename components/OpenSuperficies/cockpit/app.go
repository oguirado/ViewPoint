//go:build cockpit

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"opensuperficies.local/srf/src/kernelbook/reaper"
	"opensuperficies.local/srf/src/surfacebook/grabador"
	"opensuperficies.local/srf/src/surfacebook/manager"
)

// plazos are the per-verb timeouts of the `srf` client (manager.EjecutarSrf); an unlisted verb gets the daemon
// client's default, and srfd itself answers rc 2 for a verb it does not know.
var plazos = map[string]time.Duration{
	"estado":  10 * time.Second,
	"listar":  10 * time.Second,
	"iniciar": 60 * time.Second,
	"detener": 30 * time.Second,
}

// plazoMediacion bounds the census verb (three probe legs: Docker, procfs, ports); it is not one of the srf
// client's four verbs, so it lives outside `plazos`.
const plazoMediacion = 30 * time.Second

// App is the Go side of the cockpit: the webview's only way to srfd. It never decides anything about a request:
// admission (SO_PEERCRED), the stop signal and validation of class and slot all happen in the daemon. Beyond srfd it
// hosts the PTY stream broker (ptyserver.go), the recording supervisor (grabacion.go), the gate override ledger
// (puertas.go), host telemetry (telemetria.go) and the PBX phonebook (pbx.go).
type App struct {
	ctx          context.Context
	ptys         *PtyServer
	ptysErr      string
	grab         *grabacion
	puertas      *puertas
	telem        *telemetria
	operatorGate func(string, string, uint64) bool
}

// NewApp returns the bound application object.
func NewApp(options ...AppOption) *App {
	a := &App{grab: nuevaGrabacion(), puertas: nuevasPuertas(), telem: nuevaTelemetria()}
	for _, option := range options {
		option(a)
	}
	return a
}

func (a *App) startup(ctx context.Context) {
	a.ctx = ctx
	if err := StartState(a); err != nil {
		a.ptysErr = "DEGRADED: " + err.Error()
		return
	}
	if err := StartCooperation(a, nativeCooperationConfig(a)); err != nil {
		a.ptysErr = "DEGRADED: " + err.Error()
		StopState(a)
		return
	}
	if err := StartRecording(a, RecordingConfig{RamCapture: startRamCapture(a, ctx)}); err != nil {
		stopRamCapture(a)
		a.ptysErr = "DEGRADED: " + err.Error()
		StopCooperation(a)
		StopState(a)
		return
	}
	rp, err := reaper.NewProcessReaper()
	if err != nil {
		a.ptysErr = fmt.Sprintf("subreaper: %v", err)
		return
	}
	s, err := nuevoPtyServer(lanzarShellEnjaulado(rp))
	if err != nil {
		a.ptysErr = err.Error()
		return
	}
	a.ptys = s
}

func (a *App) shutdown(context.Context) {
	if a.grab != nil {
		a.grab.Abortar()
	}
	stopRamCapture(a)
	StopCooperation(a)
	StopState(a)
	if a.ptys != nil {
		_ = a.ptys.Cerrar()
	}
}

// PtyUrl is the SRF2 WebSocket stream of one agent's shell (VIEW-03); the shell is spawned when the stream connects.
func (a *App) PtyUrl(agentId string) (string, error) {
	if err := AuthorizeCooperationOperator(a, "pty", agentId, 0); err != nil {
		return "", err
	}
	if a.ptys == nil {
		if a.ptysErr == "" {
			return "", errors.New("pty server not started")
		}
		return "", errors.New("pty server unavailable: " + a.ptysErr)
	}
	return a.ptys.URL(agentId)
}

// IniciarGrabacion starts a forensic recording for a slot (targetXid 0: the largest mapped window of the recording
// display). It returns the session id once the four t0 artifacts exist.
func (a *App) IniciarGrabacion(slot int, targetXid uint32) (string, error) {
	return a.grab.Iniciar(slot, targetXid)
}

// DetenerGrabacion sends the clean stop and waits for the manifest seal; an unsealed manifest is an error.
func (a *App) DetenerGrabacion(sessionId string) error { return a.grab.Detener(sessionId) }

// EstadoGrabacion is the live recording state.
func (a *App) EstadoGrabacion() RecordingState { return a.grab.Estado() }

// CandidatosGrabacion lists the windows of the recording display for the target selector.
func (a *App) CandidatosGrabacion() ([]grabador.Candidato, error) { return a.grab.Candidatos() }

// CensoMediacion runs srfd's `mediacion` verb and returns its report (manager.SrfMediacionReport, not a slot list:
// the daemon's census reconciles declared and observed surfaces).
func (a *App) CensoMediacion() (*manager.SrfMediacionReport, error) {
	resp, err := manager.EnviarComando(manager.RutaSocket(""), manager.Request{Comando: "mediacion"}, plazoMediacion)
	if err != nil {
		return nil, fmt.Errorf("srfd mediacion: %w", err)
	}
	if !resp.OK {
		return nil, fmt.Errorf("srfd mediacion refused (rc %d): %s", resp.RC, resp.Error)
	}
	var informe manager.SrfMediacionReport
	if err := json.Unmarshal(resp.Datos, &informe); err != nil {
		return nil, fmt.Errorf("srfd mediacion: the payload is not a census report: %w", err)
	}
	return &informe, nil
}

// TripGate records an operator trip of one MicroVM gate (A..I) or ALL; see puertas.go for what it does not do.
func (a *App) TripGate(gateId, reason string) error {
	if err := AuthorizeCooperationOperator(a, "trip", gateId, 0); err != nil {
		return err
	}
	return a.puertas.Trip(gateId, reason)
}

// ResetGate clears the operator override of one gate or ALL.
func (a *App) ResetGate(gateId string) error {
	if err := AuthorizeCooperationOperator(a, "reset", gateId, 0); err != nil {
		return err
	}
	return a.puertas.Reset(gateId)
}

// EstadoPuertas lists the nine overrides.
func (a *App) EstadoPuertas() []GateOverride { return a.puertas.Estado() }

// TripMicroVmGate is the name TelemetryMasterView probes for; same ledger as TripGate.
func (a *App) TripMicroVmGate(gateKey, reason string) error {
	if err := AuthorizeCooperationOperator(a, "trip-microvm", gateKey, 0); err != nil {
		return err
	}
	return a.puertas.Trip(gateKey, reason)
}

// GetHostTelemetry samples this host's cores and memory (procfs, sysfs); the GPU is reported AUSENTE.
func (a *App) GetHostTelemetry() (*HostTelemetry, error) { return a.telem.Leer() }

// RespuestaSrfd is manager.Response with its raw payload decoded, so the webview receives plain JSON.
type RespuestaSrfd struct {
	OK    bool             `json:"ok"`
	RC    int              `json:"rc"`
	Error string           `json:"error,omitempty"`
	Datos any              `json:"datos,omitempty"`
	Lease *LeaseCredential `json:"lease,omitempty"`
}

// Socket is the UDS path this cockpit talks to (explicit value, $SRF_SOCKET, then the runtime directory).
func (a *App) Socket() string { return manager.RutaSocket("") }

// Enviar sends one request to srfd and returns its answer. A transport failure (no socket, daemon down,
// timeout, an answer that is not JSON) is returned as an error; a refusal by srfd (ok=false, rc 2/3/9/69...)
// is returned as an answer carrying srfd's own rc and text, never rewritten.
func (a *App) Enviar(comando, clase string, request any) (*RespuestaSrfd, error) {
	target, err := decodeLaunchRequest(request)
	if err != nil {
		return nil, err
	}
	switch comando {
	case "estado", "listar":
	case "iniciar":
		return launchGuard(a, clase, target.Slot, target.Holder, sendSrfd)
	case "detener":
		return nil, errors.New("REFUSED: use CerrarSlot")
	default:
		return nil, errors.New("REFUSED: unsupported srfd command")
	}
	return sendSrfd(comando, clase, target.Slot)
}

func sendSrfd(comando, clase string, slot uint16) (*RespuestaSrfd, error) {
	resp, err := manager.EnviarComando(manager.RutaSocket(""), manager.Request{Comando: comando, Clase: clase, Slot: slot}, plazos[comando])
	if err != nil {
		return nil, fmt.Errorf("srfd %s: %w", comando, err)
	}
	salida := &RespuestaSrfd{OK: resp.OK, RC: resp.RC, Error: resp.Error}
	if len(resp.Datos) > 0 {
		if err := json.Unmarshal(resp.Datos, &salida.Datos); err != nil {
			return nil, fmt.Errorf("srfd %s: the payload is not JSON: %w", comando, err)
		}
	}
	return salida, nil
}
