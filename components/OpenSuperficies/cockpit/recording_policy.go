//go:build cockpit

package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/wailsapp/wails/v2/pkg/menu"
	"github.com/wailsapp/wails/v2/pkg/runtime"
)

// RamCapture implementations MUST use vadapt.ramws, honor context cancellation,
// and never persist frames. Reap must also handle a failed/empty session identity.
// No production implementation is available in this corpus.
type RamCapture interface {
	Start(context.Context, int, uint32, int64) (string, error)
	Stop(context.Context, string) error
	Reap(context.Context, string) error
	SizeBytes(context.Context, string) (int64, error)
}
type Exporter interface {
	Export(context.Context, string, string) (string, error)
}
type SaveDialogRequest struct {
	SlotID, TargetDir, Title string
	SizeBytes                int64
	Buttons                  []string
}
type SaveDialogChoice struct{ Action, TargetDir string }
type RecordingConfig struct {
	RamCapture RamCapture
	Exporter   Exporter
	Dialog     func(SaveDialogRequest) (SaveDialogChoice, error)
	TTLms      int64
}
type recordingPolicy struct {
	app    *App
	config RecordingConfig
}

// StartRecording follows StartState -> StartCooperation. It changes only the
// recording projection; canonical state retains the configured TTL, never a clock.
func StartRecording(a *App, cfg RecordingConfig) error {
	if a == nil || a.grab == nil {
		return errors.New("AUSENTE: recording supervisor")
	}
	if cfg.TTLms == 0 {
		cfg.TTLms = 1800000
	}
	if cfg.TTLms < 1 || cfg.TTLms > int64((1<<63-1)/int64(time.Millisecond)) {
		return errors.New("invalid recording TTL")
	}
	if cooperationFor(a) == nil {
		return cooperationMissing()
	}
	stateServers.Lock()
	defer stateServers.Unlock()
	s := stateServers.items[a]
	if s == nil {
		return errors.New("AUSENTE: state producer")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	g := a.grab
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.policy != nil {
		return errors.New("recording already started")
	}
	p := &recordingPolicy{app: a, config: cfg}
	g.policy = p
	base := s.source
	s.source = func() (stateInput, error) {
		in, err := base()
		if err != nil {
			return in, err
		}
		rs := a.grab.Estado()
		in.Capsules = append([]stateCapsuleInput(nil), in.Capsules...)
		for i := range in.Capsules {
			v := &in.Capsules[i]
			v.Recording = StateRecording{Phase: "idle"}
			if v.Slot == rs.Slot && rs.Fase != "" && rs.Fase != "idle" {
				v.Recording = StateRecording{Phase: rs.Fase, Store: rs.Store, TTL: rs.TTL}
			}
		}
		return in, nil
	}
	return nil
}
func recordingGuard(c *cooperationStore, kind, id string, epoch uint64, cred *LeaseCredential) (StateCapsule, error) {
	s, err := c.slot(id, epoch)
	if err != nil {
		return s, err
	}
	if cred != nil || s.Mode == "agent_locked" {
		return s, c.authenticate(s, cred)
	}
	if s.Mode == "read_only" {
		return s, fmt.Errorf("REFUSED: %s LEASE-REQUIRED", id)
	}
	if s.Mode != "operator_controlled" {
		return s, errors.New("REFUSED: SWITCHING")
	}
	return s, c.operator(kind, id, epoch)
}
func (a *App) IniciarGrabacionSlot(id string, xid uint32, epoch uint64, cred *LeaseCredential) (string, error) {
	c := cooperationFor(a)
	if c == nil {
		return "", cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	s, err := recordingGuard(c, "rec-start", id, epoch, cred)
	if err != nil {
		return "", err
	}
	a.grab.mu.Lock()
	p := a.grab.policy
	a.grab.mu.Unlock()
	if p == nil {
		return "", errors.New("AUSENTE: recording policy")
	}
	session, err := a.grab.startRAM(s.Slot, xid)
	updateRecordingMenu(a)
	return session, err
}
func (a *App) DetenerGrabacionSlot(id, session string, epoch uint64, cred *LeaseCredential) error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	s, err := recordingGuard(c, "rec-stop", id, epoch, cred)
	if err != nil {
		return err
	}
	rs := a.grab.Estado()
	if s.Slot != rs.Slot || session == "" || session != rs.SessionID {
		return errors.New("REFUSED: recording session mismatch")
	}
	err = a.grab.stopRAM(session)
	updateRecordingMenu(a)
	return err
}

// WIRE composes this FIRST in CooperationConfig.Stop. Abort has no authority
// guard (OQ3), and this function can only seal RAM, never export it.
func stopRecordingForAbort(a *App, id string) error {
	if a == nil || a.grab == nil {
		return nil
	}
	rs := a.grab.Estado()
	if id != "" && id != cooperationID(rs.Slot) {
		return nil
	}
	if rs.Fase != "recording" && rs.Fase != "starting" && rs.Fase != "stopping" {
		return nil
	}
	err := a.grab.stopRAM(rs.SessionID)
	updateRecordingMenu(a)
	return err
}

// Only a native menu callback calls this package function. It MUST NOT become an
// App method, binding, HTTP handler or lease action.
func recordingSavePreview(a *App) (*recordingPolicy, RecordingState, SaveDialogRequest, error) {
	var empty RecordingState
	var request SaveDialogRequest
	if a == nil || a.grab == nil {
		return nil, empty, request, errors.New("AUSENTE: recording policy")
	}
	c := cooperationFor(a)
	if c == nil {
		return nil, empty, request, cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	g := a.grab
	g.operation.Lock()
	defer g.operation.Unlock()
	g.mu.Lock()
	p, target := g.policy, g.raiz
	g.mu.Unlock()
	if p == nil {
		return nil, empty, request, errors.New("AUSENTE: recording policy")
	}
	rs := g.Estado()
	if rs.Fase != "sealed" || rs.Store == nil || *rs.Store != "ram" || rs.TTL == nil {
		return nil, rs, request, errors.New("REFUSED: save requires sealed RAM")
	}
	d, err := c.document()
	if err != nil {
		return nil, rs, request, err
	}
	var slot *StateCapsule
	for i := range d.Capsules {
		if d.Capsules[i].Slot == rs.Slot {
			slot = &d.Capsules[i]
			break
		}
	}
	if slot == nil {
		return nil, rs, request, errors.New("REFUSED: UNKNOWN-SLOT")
	}
	if slot.Mode != "operator_controlled" || slot.LeaseHolder != nil {
		return nil, rs, request, errors.New("REFUSED: OPERATOR-AUTHORITY")
	}
	if err = c.operator("rec-save", slot.SlotID, slot.LeaseEpoch); err != nil {
		return nil, rs, request, err
	}
	if p.config.Dialog == nil && a.ctx == nil {
		return nil, rs, request, errors.New("AUSENTE: native save dialog")
	}
	if p.config.Exporter == nil {
		return nil, rs, request, errors.New("AUSENTE: RAM exporter")
	}
	ctx, cancel := recordingContext()
	defer cancel()
	size, err := p.config.RamCapture.SizeBytes(ctx, rs.SessionID)
	if err != nil {
		return nil, rs, request, errors.New(recordingError(err))
	}
	if size <= 0 {
		return nil, rs, request, errors.New("AUSENTE: RAM size")
	}
	if target == "" || !filepath.IsAbs(target) {
		return nil, rs, request, errors.New("AUSENTE: save target directory")
	}
	request = SaveDialogRequest{SlotID: slot.SlotID, TargetDir: target, SizeBytes: size, Title: "Guardar grabación " + slot.SlotID, Buttons: []string{"Guardar", "Elegir directorio…", "Cancelar"}}
	return p, rs, request, nil
}
func triggerOperatorSave(a *App) error {
	p, preview, req, err := recordingSavePreview(a)
	if err != nil {
		return err
	}
	// Never hold lifecycle or cooperation locks across a human dialog: TTL reap
	// and emergency abort must remain operable while the menu is open.
	dialog := p.config.Dialog
	if dialog == nil {
		dialog = func(r SaveDialogRequest) (SaveDialogChoice, error) { return nativeRecordingDialog(a, r) }
	}
	choice, err := dialog(req)
	if err != nil {
		return errors.New(recordingError(err))
	}
	if choice.Action == "Cancelar" || choice.Action == "" {
		return nil
	}
	if choice.Action != "Guardar" || !filepath.IsAbs(choice.TargetDir) {
		return errors.New("REFUSED: save not confirmed")
	}
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	g := a.grab
	g.operation.Lock()
	defer g.operation.Unlock()
	rs := g.Estado()
	if rs.SessionID != preview.SessionID || rs.Fase != "sealed" || rs.Store == nil || *rs.Store != "ram" || rs.TTL == nil || rs.SinceMonoMs >= *rs.TTL {
		return errors.New("REFUSED: RAM expired or changed")
	}
	d, err := c.document()
	if err != nil {
		return err
	}
	var current *StateCapsule
	for i := range d.Capsules {
		if d.Capsules[i].SlotID == req.SlotID {
			current = &d.Capsules[i]
			break
		}
	}
	if current == nil || current.Mode != "operator_controlled" || current.LeaseHolder != nil {
		return errors.New("REFUSED: OPERATOR-AUTHORITY")
	}
	if err = c.operator("rec-save", current.SlotID, current.LeaseEpoch); err != nil {
		return err
	}
	exportCtx, cancel := recordingContext()
	defer cancel()
	dir, err := p.config.Exporter.Export(exportCtx, rs.SessionID, choice.TargetDir)
	if err != nil {
		return errors.New(recordingError(err))
	}
	if err = verifyRecordingExport(choice.TargetDir, dir); err != nil {
		return err
	}
	if err = p.config.RamCapture.Reap(exportCtx, rs.SessionID); err != nil {
		return errors.New(recordingError(err))
	}
	disk := "disk"
	g.mu.Lock()
	g.store, g.ttl, g.dir = &disk, nil, dir
	if g.timer != nil {
		g.timer.Stop()
	}
	g.mu.Unlock()
	updateRecordingMenu(a)
	return nil
}
func verifyRecordingExport(root, dir string) error {
	// Resolve directories before accepting artifacts; no symlink escape or invented receipt.
	base, err := filepath.EvalSymlinks(root)
	if err != nil {
		return errors.New("unverified export directory")
	}
	resolved, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return errors.New("unverified export directory")
	}
	rel, err := filepath.Rel(base, resolved)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return errors.New("export outside confirmed directory")
	}
	for _, name := range []string{"video-000.mp4", "eventos.jsonl", "FLUJO.md", "manifiesto.json"} {
		st, err := os.Lstat(filepath.Join(resolved, name))
		if err != nil || !st.Mode().IsRegular() || st.Size() <= 0 {
			return errors.New("unverified OpenFresnel artifacts")
		}
	}
	return nil
}
func nativeRecordingDialog(a *App, req SaveDialogRequest) (SaveDialogChoice, error) {
	if a.ctx == nil {
		return SaveDialogChoice{}, errors.New("AUSENTE: native save dialog")
	}
	for range 5 {
		action, err := runtime.MessageDialog(a.ctx, runtime.MessageDialogOptions{Type: runtime.QuestionDialog, Title: req.Title, Message: fmt.Sprintf("%s\n%d bytes", req.TargetDir, req.SizeBytes), Buttons: req.Buttons, DefaultButton: "Cancelar", CancelButton: "Cancelar"})
		if err != nil {
			return SaveDialogChoice{}, err
		}
		if action != "Elegir directorio…" {
			return SaveDialogChoice{Action: action, TargetDir: req.TargetDir}, nil
		}
		dir, err := runtime.OpenDirectoryDialog(a.ctx, runtime.OpenDialogOptions{Title: req.Title})
		if err != nil {
			return SaveDialogChoice{}, err
		}
		if dir == "" {
			return SaveDialogChoice{Action: "Cancelar"}, nil
		}
		req.TargetDir = dir
	}
	return SaveDialogChoice{}, errors.New("REFUSED: save dialog limit")
}
func recMenu(a *App) *menu.Menu {
	m := menu.NewMenu()
	item := m.AddText("Guardar grabación en disco…", nil, func(*menu.CallbackData) {
		if err := triggerOperatorSave(a); err != nil && a.ctx != nil {
			runtime.LogError(a.ctx, recordingError(err))
		}
	})
	item.Disabled = true
	a.grab.mu.Lock()
	a.grab.nativeMenu = item
	a.grab.mu.Unlock()
	updateRecordingMenu(a)
	return m
}
func updateRecordingMenu(a *App) {
	g := a.grab
	g.mu.Lock()
	p := g.policy
	if p == nil || g.nativeMenu == nil {
		g.mu.Unlock()
		return
	}
	g.nativeMenu.Disabled = g.fase != "sealed" || g.store == nil || *g.store != "ram"
	g.mu.Unlock()
	if a.ctx != nil {
		runtime.MenuUpdateApplicationMenu(a.ctx)
	}
}
