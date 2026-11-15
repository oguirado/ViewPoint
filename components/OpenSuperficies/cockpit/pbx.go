//go:build cockpit

package main

import (
	"errors"
	"fmt"
	"strings"
	"syscall"

	"opensuperficies.local/srf/src/surfacebook/pbx"
)

// The PBX switchboard from the cockpit: the phonebook (src/surfacebook/pbx) drives the OpenSwitchboard `sbd` CLI,
// the only path to the daemon the corpus offers. The three tactile controls of VIEW-02 map onto the verbs the
// phonebook exposes; the mapping is the bridge's, stated here: JACK takes the extension off hold (sbd hold --off,
// CONNECTED), TRUNK puts it on hold (sbd hold, ON_HOLD), MONITOR observes it (sbd snoop). "RELEASE" has no daemon
// verb: AUSENTE, the view clears its table locally.

func agenda() *pbx.PhonebookManager {
	return pbx.NuevoPhonebookManager(pbx.RutaAgendaPorDefecto())
}

// PbxLeases lists the extensions of the phonebook (DIAL IN of VIEW-02).
func (a *App) PbxLeases() ([]*pbx.Extension, error) {
	return agenda().Listar()
}

// PbxAccion carries out one tactile control on one extension slug: jack, trunk or monitor.
func (a *App) PbxAccion(accion, slug string) (*pbx.Extension, error) {
	slug = strings.TrimSpace(slug)
	if slug == "" {
		return nil, fmt.Errorf("pbx: an extension slug is required")
	}
	switch strings.ToLower(strings.TrimSpace(accion)) {
	case "jack":
		if err := a.pbxOperator("jack", slug); err != nil {
			return nil, err
		}
		return agenda().Hold(slug, true)
	case "trunk":
		if err := a.pbxOperator("trunk", slug); err != nil {
			return nil, err
		}
		return agenda().Hold(slug, false)
	case "monitor":
		if err := a.pbxOperator("monitor", slug); err != nil {
			return nil, err
		}
		return agenda().Snoop(slug)
	}
	return nil, fmt.Errorf("pbx: unknown tactile action %q (jack, trunk, monitor)", accion)
}

// CooperationPBX contains native host actuators. No simulated barge path is used.
// Dispatch and Divert must be available before stopping a process group.
type CooperationPBX struct {
	List     func() ([]*pbx.Extension, error)
	Tick     func(int) (uint64, error)
	Signal   func(int, syscall.Signal) error
	Dispatch func(string) error
	Divert   func(string) error
	Act      func(slug, act, text string) error
}

func (a *App) pbxOperator(kind, slug string) error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	return c.operator(kind, slug, 0)
}
func (a *App) PbxWhisper(slug, msg string) error {
	if !stateSlugPattern.MatchString(slug) || strings.TrimSpace(msg) == "" {
		return errors.New("REFUSED: whisper target or message")
	}
	if err := a.pbxOperator("whisper", slug); err != nil {
		return err
	}
	return agenda().Whisper(slug, msg)
}
func (a *App) PbxHangup(slug string, extension int) error {
	if err := a.pbxOperator("hangup", slug); err != nil {
		return err
	}
	return errors.New("failed: AUSENTE: PBX hangup backend")
}
func (a *App) PbxPublicarActo(slug, act, text string) error {
	if err := a.pbxOperator("act", slug); err != nil {
		return err
	}
	switch act {
	case "open", "claim", "refute", "evidence", "question", "answer", "vote", "handoff", "verdict", "ack":
	default:
		return errors.New("REFUSED: unknown PBX act")
	}
	if !stateSlugPattern.MatchString(slug) || strings.TrimSpace(text) == "" {
		return errors.New("REFUSED: act target or text")
	}
	if act == "claim" && !strings.Contains(text, "[MEASURED]") && !strings.Contains(text, "[CITED]") && !strings.Contains(text, "[TARGET]") {
		return errors.New("REFUSED: claim requires provenance")
	}
	if act == "refute" && !strings.Contains(text, "evidence:") {
		return errors.New("REFUSED: refute requires evidence:")
	}
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	if c.config.PBX == nil || c.config.PBX.Act == nil {
		return errors.New("failed: AUSENTE: PBX composer backend")
	}
	return c.config.PBX.Act(slug, act, text)
}
func (a *App) PbxBarge(slug string, extTyped int) error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	if err := c.operator("barge", slug, 0); err != nil {
		return err
	}
	backend := c.config.PBX
	if backend == nil || backend.Dispatch == nil || backend.Divert == nil || c.config.Revoke == nil || c.config.SetMode == nil {
		return errors.New("failed: AUSENTE: PBX barge backend")
	}
	list := backend.List
	if list == nil {
		list = agenda().Listar
	}
	extensions, err := list()
	if err != nil {
		return err
	}
	var target *pbx.Extension
	for _, e := range extensions {
		if e.Slug == slug || e.AgenteSlug == slug {
			target = e
			break
		}
	}
	if target == nil || target.Extension != extTyped || target.PID <= 1 || target.StartTimeTick == 0 {
		return errors.New("REFUSED: extension identity")
	}
	tick := backend.Tick
	if tick == nil {
		tick = pbx.ObtenerStartTimeProcfs
	}
	observed, err := tick(target.PID)
	if err != nil || observed != target.StartTimeTick {
		return errors.New("REFUSED: process start tick mismatch")
	}
	if err = backend.Dispatch(slug); err != nil {
		return err
	}
	// Recheck directly before the signal to reject recycled identities.
	observed, err = tick(target.PID)
	if err != nil || observed != target.StartTimeTick {
		return errors.New("REFUSED: process start tick mismatch")
	}
	signal := backend.Signal
	if signal == nil {
		signal = syscall.Kill
	}
	if err = signal(-target.PID, syscall.SIGSTOP); err != nil {
		return err
	}
	doc, err := c.document()
	if err != nil {
		return err
	}
	for _, s := range doc.Capsules {
		if s.LeaseHolder != nil && *s.LeaseHolder == slug {
			if err = c.revoke(s); err != nil {
				return err
			}
			if c.config.SetMode == nil {
				return errors.New("failed: AUSENTE: mode actuator")
			}
			if err = c.config.SetMode(s.SlotID, "operator_controlled"); err != nil {
				return err
			}
			c.mu.Lock()
			delete(c.overlay, s.SlotID)
			c.mu.Unlock()
		}
	}
	if err = backend.Divert(slug); err != nil {
		return err
	}
	return c.journal("barge", slug)
}
