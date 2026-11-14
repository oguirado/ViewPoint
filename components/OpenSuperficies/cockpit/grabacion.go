//go:build cockpit

package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"

	"github.com/wailsapp/wails/v2/pkg/menu"

	"opensuperficies.local/srf/src/surfacebook/grabador"
)

// No recorder process or disk fallback lives here. A native host must inject a
// vadapt.ramws capture through StartRecording before any binding can record.
const displayDefecto = ":1"

type RecordingState struct {
	Recording   bool    `json:"recording"`
	SessionID   string  `json:"sessionId"`
	TargetXid   string  `json:"targetXid"`
	SinceMonoMs int64   `json:"sinceMonoMs"`
	OffsetMs    *int64  `json:"offsetMs"`
	Verdict     string  `json:"verdict"`
	Fase        string  `json:"fase"`
	Slot        int     `json:"slot"`
	Dir         string  `json:"dir"`
	Display     string  `json:"display"`
	Manifiesto  string  `json:"manifiesto"`
	Artefactos  int     `json:"artefactos"`
	DuracionMs  int64   `json:"duracionMs"`
	Error       string  `json:"error,omitempty"`
	Store       *string `json:"store"`
	TTL         *int64  `json:"ttl_ms"`
}

type grabacion struct {
	nativeMenu                *menu.MenuItem
	mu                        sync.Mutex
	operation                 sync.Mutex
	raiz, display, bin        string
	fase, sessionID, dir, xid string
	slot                      int
	inicio                    time.Time
	duracion                  int64
	err                       string
	store                     *string
	ttl                       *int64
	timer                     *time.Timer
	policy                    *recordingPolicy
}

func raizRepo() string {
	if r := os.Getenv("SRF_RAIZ"); r != "" {
		return r
	}
	starts := []string{}
	if e, err := os.Executable(); err == nil {
		starts = append(starts, filepath.Dir(e))
	}
	if w, err := os.Getwd(); err == nil {
		starts = append(starts, w)
	}
	for _, d := range starts {
		for range 8 {
			if st, err := os.Stat(filepath.Join(d, "cmd", "srf-grabar")); err == nil && st.IsDir() {
				return d
			}
			p := filepath.Dir(d)
			if p == d {
				break
			}
			d = p
		}
	}
	return ""
}
func nuevaGrabacion() *grabacion {
	g := &grabacion{fase: "idle", display: displayDefecto}
	if r := raizRepo(); r != "" {
		g.bin = filepath.Join(r, "bin", "srf-grabar")
		g.raiz = filepath.Join(r, "videos", "srf-ng")
	}
	if r := os.Getenv("SRF_GRABAR_RAIZ"); r != "" {
		g.raiz = r
	}
	return g
}
func (g *grabacion) activa() bool {
	return g.fase == "starting" || g.fase == "recording" || g.fase == "stopping"
}

// Legacy bindings use the same authority path as the explicit slot bindings.
func (g *grabacion) Iniciar(slot int, xid uint32) (string, error) {
	g.mu.Lock()
	p := g.policy
	g.mu.Unlock()
	if p == nil {
		return "", errors.New("AUSENTE: recording policy")
	}
	c := cooperationFor(p.app)
	if c == nil {
		return "", cooperationMissing()
	}
	d, err := c.document()
	if err != nil {
		return "", err
	}
	for _, s := range d.Capsules {
		if s.Slot == slot {
			return p.app.IniciarGrabacionSlot(s.SlotID, xid, s.LeaseEpoch, nil)
		}
	}
	return "", errors.New("REFUSED: UNKNOWN-SLOT")
}
func (g *grabacion) Detener(id string) error {
	g.mu.Lock()
	p, slot := g.policy, g.slot
	g.mu.Unlock()
	if p == nil {
		return errors.New("AUSENTE: recording policy")
	}
	c := cooperationFor(p.app)
	if c == nil {
		return cooperationMissing()
	}
	d, err := c.document()
	if err != nil {
		return err
	}
	for _, s := range d.Capsules {
		if s.Slot == slot {
			return p.app.DetenerGrabacionSlot(s.SlotID, id, s.LeaseEpoch, nil)
		}
	}
	return errors.New("REFUSED: UNKNOWN-SLOT")
}
func (g *grabacion) Abortar() {
	g.mu.Lock()
	if g.timer != nil {
		g.timer.Stop()
	}
	p := g.policy
	g.mu.Unlock()
	if p != nil {
		_ = stopRecordingForAbort(p.app, "")
		_ = p.reap()
	}
}

var recordingPaths = regexp.MustCompile(`(?:/[A-Za-z0-9_.~:+-]+)+|[A-Za-z]:\\[^\s]+`)

func recordingError(err error) string {
	if err == nil {
		return ""
	}
	return recordingPaths.ReplaceAllString(err.Error(), "<path>")
}
func (g *grabacion) Estado() RecordingState {
	g.mu.Lock()
	defer g.mu.Unlock()
	e := RecordingState{Fase: g.fase, SessionID: g.sessionID, TargetXid: g.xid, Slot: g.slot, Dir: g.dir, Display: g.display, Error: recordingError(errors.New(g.err)), Verdict: "IDLE", DuracionMs: g.duracion}
	if g.store != nil {
		v := *g.store
		e.Store = &v
	}
	if g.ttl != nil {
		v := *g.ttl
		e.TTL = &v
	}
	if !g.inicio.IsZero() && (g.activa() || (g.fase == "sealed" && g.store != nil && *g.store == "ram")) {
		e.SinceMonoMs = time.Since(g.inicio).Milliseconds()
	}
	switch g.fase {
	case "starting":
		e.Verdict = "STARTING"
	case "recording", "stopping":
		e.Recording = true
		e.Verdict = "RECORDING"
	case "sealed":
		e.Verdict = "SEALED"
	case "error":
		e.Verdict = "INCOMPLETA"
	}
	if g.fase == "sealed" && g.store != nil && *g.store == "disk" {
		e.Manifiesto = "completa"
		e.Artefactos = 4
	}
	return e
}
func (g *grabacion) Candidatos() ([]grabador.Candidato, error) {
	return grabador.CandidatosDeDisplay(g.display, 0)
}

func recordingContext() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 15*time.Second)
}
func (g *grabacion) fail(err error) error {
	clean := recordingError(err)
	g.mu.Lock()
	g.fase, g.err = "error", clean
	g.mu.Unlock()
	return errors.New(clean)
}
func (g *grabacion) startRAM(slot int, xid uint32) (string, error) {
	g.operation.Lock()
	defer g.operation.Unlock()
	g.mu.Lock()
	p := g.policy
	if g.activa() || (g.store != nil && *g.store == "ram") {
		g.mu.Unlock()
		return "", errors.New("REFUSED: recording session retained")
	}
	if p.config.RamCapture == nil {
		g.slot, g.store, g.ttl, g.dir = slot, nil, nil, ""
		g.mu.Unlock()
		return "", g.fail(errors.New("AUSENTE: RAM capture"))
	}
	ttl := p.config.TTLms
	ram := "ram"
	g.fase, g.slot, g.xid, g.dir, g.err = "starting", slot, grabador.Hex(uint64(xid)), "", ""
	g.store, g.ttl, g.inicio = &ram, &ttl, time.Now()
	g.timer = time.AfterFunc(time.Duration(ttl)*time.Millisecond, func() { _ = p.reap() })
	g.duracion = 0
	g.sessionID = ""
	g.mu.Unlock()
	ctx, cancel := recordingContext()
	defer cancel()
	id, err := p.config.RamCapture.Start(ctx, slot, xid, ttl)
	g.mu.Lock()
	g.sessionID = id
	g.mu.Unlock()
	if err != nil {
		return "", g.fail(err)
	}
	if id == "" {
		return "", g.fail(errors.New("AUSENTE: RAM session identity"))
	}
	g.mu.Lock()
	g.sessionID, g.fase = id, "recording"

	g.mu.Unlock()
	return id, nil
}
func (g *grabacion) stopRAM(id string) error {
	g.operation.Lock()
	defer g.operation.Unlock()
	g.mu.Lock()
	if !g.activa() {
		g.mu.Unlock()
		return errors.New("REFUSED: no active recording")
	}
	if id != "" && id != g.sessionID {
		g.mu.Unlock()
		return errors.New("REFUSED: recording session mismatch")
	}
	p, session := g.policy, g.sessionID
	g.fase = "stopping"
	g.mu.Unlock()
	ctx, cancel := recordingContext()
	defer cancel()
	if err := p.config.RamCapture.Stop(ctx, session); err != nil {
		return g.fail(err)
	}
	g.mu.Lock()
	g.fase, g.duracion = "sealed", time.Since(g.inicio).Milliseconds()
	g.mu.Unlock()
	return nil
}
func (p *recordingPolicy) reap() error {
	g := p.app.grab
	g.operation.Lock()
	defer func() { g.operation.Unlock(); updateRecordingMenu(p.app) }()
	g.mu.Lock()
	if g.store == nil || *g.store != "ram" {
		g.mu.Unlock()
		return nil
	}
	id := g.sessionID
	g.mu.Unlock()
	ctx, cancel := recordingContext()
	defer cancel()
	if err := p.config.RamCapture.Reap(ctx, id); err != nil {
		return g.fail(fmt.Errorf("RAM reap: %w", err))
	}
	g.mu.Lock()
	if g.timer != nil {
		g.timer.Stop()
	}
	g.fase, g.store, g.ttl, g.dir, g.sessionID, g.err = "idle", nil, nil, "", "", ""
	g.inicio = time.Time{}
	g.mu.Unlock()
	return nil
}
