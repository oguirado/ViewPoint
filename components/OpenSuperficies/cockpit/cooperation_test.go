//go:build cockpit

package main

import (
	"encoding/json"
	"errors"
	"opensuperficies.local/srf/src/surfacebook/pbx"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func cooperationFixture(t *testing.T) (*App, *CooperationConfig, *stateInput) {
	t.Helper()
	t.Setenv("XDG_RUNTIME_DIR", t.TempDir())
	a := NewApp()
	in := &stateInput{Host: "localhost", WindowMode: "column_1_3_dual", Cap: 2, Capsules: []stateCapsuleInput{{Slot: 1, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "operator_controlled"}, {Slot: 2, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "read_only"}}}
	s := &stateServer{app: a, epoch: time.Now(), source: func() (stateInput, error) { return *in, nil }}
	stateServers.Lock()
	stateServers.items[a] = s
	stateServers.Unlock()
	cfg := &CooperationConfig{Operator: func(string, string, uint64) bool { return true }, Clip: func(string) error { return nil }, SetMode: func(id, mode string) error {
		for i := range in.Capsules {
			if cooperationID(in.Capsules[i].Slot) == id {
				in.Capsules[i].Mode = mode
				return nil
			}
		}
		return errors.New("missing")
	}, Stop: func(id string) error {
		for i := range in.Capsules {
			if cooperationID(in.Capsules[i].Slot) == id {
				in.Capsules[i].Lifecycle = "stopped"
				return nil
			}
		}
		return errors.New("missing")
	}}
	cfg.Revoke = func(id string, epoch uint64) error {
		for i := range in.Capsules {
			if cooperationID(in.Capsules[i].Slot) == id {
				in.Capsules[i].LeaseHolder = nil
				in.Capsules[i].LeaseEpoch = epoch + 1
				in.Capsules[i].Mode = "read_only"
				return nil
			}
		}
		return errors.New("missing")
	}
	if err := StartCooperation(a, *cfg); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { StopCooperation(a); stateServers.Lock(); delete(stateServers.items, a); stateServers.Unlock() })
	return a, cfg, in
}
func TestCooperationCopyBytes(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	cooperationFor(a).config.Clip = nil
	got, err := a.CopiarSlot("srf-ng-1", "bloque")
	if err != nil {
		t.Fatal(err)
	}
	if got.Bytes != a.BloqueSlot("srf-ng-1") {
		t.Fatal("copy differs from producer")
	}
	var c StateCapsule
	if json.Unmarshal([]byte(got.Bytes), &c) != nil || got.Seq != c.Seq {
		t.Fatal("copy seq")
	}
	if _, err = a.CopiarSlot("srf-ng-1", "invalid"); err == nil {
		t.Fatal("invalid kind accepted")
	}
}
func TestCooperationGuardAndEpoch(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	if err := a.CederControl("srf-ng-1", 1); err == nil || !strings.Contains(err.Error(), "STALE-EPOCH") {
		t.Fatal("stale accepted", err)
	}
	cooperationFor(a).config.Operator = nil
	if err := a.CederControl("srf-ng-1", 0); err == nil {
		t.Fatal("untokened handoff")
	}
	if err := a.CerrarSlot("srf-ng-1", 0); err == nil {
		t.Fatal("untokened close")
	}
	if err := a.TomarControl("srf-ng-1", 0); err == nil {
		t.Fatal("untokened takeover")
	}
	if _, err := a.SolicitarArriendo("srf-ng-1", "agent", 0); err == nil || !strings.Contains(err.Error(), "OPERATOR-CONTROL") {
		t.Fatal("agent in Op", err)
	}
}
func TestCooperationEndToEndStateSeq(t *testing.T) {
	a, _, in := cooperationFixture(t)
	holder := "agent"
	in.Capsules[1].Mode = "agent_locked"
	in.Capsules[1].LeaseHolder = &holder
	in.Capsules[1].LeaseEpoch = 4
	before := a.EstadoCanonico()
	if err := a.AbortarTodo(); err != nil {
		t.Fatal(err)
	}
	var d StateDocument
	if json.Unmarshal([]byte(a.EstadoCanonico()), &d) != nil {
		t.Fatal("state")
	}
	for _, c := range d.Capsules {
		if c.LeaseHolder != nil || c.Lifecycle != "stopped" {
			t.Fatal("abort incomplete")
		}
		if c.Slot == 2 && c.LeaseEpoch <= 4 {
			t.Fatal("epoch not bumped")
		}
	}
	if before == a.EstadoCanonico() {
		t.Fatal("seq unchanged")
	}
	cooperationFor(a).config.Stop = func(string) error { return errors.New("stop failed") }
	if err := a.AbortarTodo(); err == nil || !strings.Contains(err.Error(), "srf-ng-1") {
		t.Fatal("abort swallowed failure", err)
	}
}
func TestOverlayNeverFabricatesHolder(t *testing.T) {
	a, _, in := cooperationFixture(t)
	c := cooperationFor(a)
	c.mu.Lock()
	c.overlay["srf-ng-1"] = cooperationOverlay{Epoch: 7, ReadOnly: true}
	c.mu.Unlock()
	var doc StateDocument
	json.Unmarshal([]byte(a.EstadoCanonico()), &doc)
	if doc.Capsules[0].LeaseHolder != nil || doc.Capsules[0].Mode != "read_only" || doc.Capsules[0].LeaseEpoch != 7 {
		t.Fatal("overlay widened")
	}
	in.Capsules[0].Mode = "agent_locked"
	holder := "real"
	in.Capsules[0].LeaseHolder = &holder
	in.Capsules[0].LeaseEpoch = 8
	if a.EstadoCanonico() != "" {
		t.Fatal("conflicting authority guessed")
	}
}
func TestCooperationLayoutAndStorage(t *testing.T) {
	a, _, in := cooperationFixture(t)
	// Operator layout requires all globally affected slots to be in Op mode.
	in.Capsules[1].Mode = "operator_controlled"
	if err := a.FijarDisposicion(1, "2x2", 0, nil); err == nil {
		t.Fatal("in-window 2x2")
	}
	if err := a.FijarDisposicion(1, "1x2", 0, nil); err != nil {
		t.Fatal(err)
	}
	st, err := os.Stat(filepath.Join(os.Getenv("XDG_RUNTIME_DIR"), "os-cu", "cooperation.wal"))
	if err != nil || st.Mode().Perm() != 0600 {
		t.Fatal("unsafe WAL", err)
	}
	ty := reflect.TypeOf(a)
	for i := 0; i < ty.NumMethod(); i++ {
		n := ty.Method(i).Name
		if strings.HasPrefix(n, "Guardar") || strings.HasPrefix(n, "Save") {
			t.Fatal("disk binding")
		}
	}
}
func TestCooperationUnavailableAndPBX(t *testing.T) {
	a := NewApp()
	if err := a.AbortarTodo(); err == nil {
		t.Fatal("absent store")
	}
	a, _, _ = cooperationFixture(t)
	cooperationFor(a).config.Operator = nil
	if _, err := a.PbxAccion("jack", "agent"); err == nil {
		t.Fatal("legacy unguarded jack")
	}
	if err := a.PbxHangup("agent", 100); err == nil {
		t.Fatal("invented hangup")
	}
	if err := a.PbxPublicarActo("agent", "propose", "x"); err == nil {
		t.Fatal("invented act")
	}
}

func TestCooperationLeaseAuthentication(t *testing.T) {
	a, _, in := cooperationFixture(t)
	c := cooperationFor(a)
	c.config.Grant = func(id, holder string, epoch uint64) error {
		for i := range in.Capsules {
			if cooperationID(in.Capsules[i].Slot) == id {
				in.Capsules[i].Mode = "agent_locked"
				in.Capsules[i].LeaseHolder = &holder
				in.Capsules[i].LeaseEpoch = epoch
				return nil
			}
		}
		return errors.New("unknown")
	}
	if err := a.CederControl("srf-ng-1", 0); err != nil {
		t.Fatal(err)
	}
	cred, err := a.SolicitarArriendo("srf-ng-1", "agent", 0)
	if err != nil {
		t.Fatal(err)
	}
	slot, err := c.slot("srf-ng-1", cred.Epoch)
	if err != nil {
		t.Fatal(err)
	}
	if err = c.authenticate(slot, &cred); err != nil {
		t.Fatal(err)
	}
	forged := cred
	forged.Token = strings.Repeat("0", 32)
	if c.authenticate(slot, &forged) == nil {
		t.Fatal("forged token accepted")
	}
	forged = cred
	forged.Epoch++
	if c.authenticate(slot, &forged) == nil {
		t.Fatal("stale epoch accepted")
	}
	if err = a.CerrarSlot(slot.SlotID, cred.Epoch); err == nil {
		t.Fatal("close with active holder")
	}
	if err = a.TomarControl(slot.SlotID, cred.Epoch); err != nil {
		t.Fatal(err)
	}
	if c.lessor.AutenticarToken(slot.SlotID, uint32(slot.Slot), cred.Epoch, c.sessions[slot.SlotID].Token) {
		t.Fatal("revoked token survived")
	}
}

func TestPbxBargeRealLeaseAndComposerActs(t *testing.T) {
	a, _, in := cooperationFixture(t)
	c := cooperationFor(a)
	holder := "agent"
	in.Capsules[1].Mode = "agent_locked"
	in.Capsules[1].LeaseHolder = &holder
	in.Capsules[1].LeaseEpoch = 4
	steps := []string{}
	c.config.PBX = &CooperationPBX{
		List: func() ([]*pbx.Extension, error) {
			return []*pbx.Extension{{Slug: "agent", Extension: 321, PID: 99, StartTimeTick: 123}}, nil
		},
		Tick:     func(int) (uint64, error) { return 123, nil },
		Dispatch: func(string) error { steps = append(steps, "dispatch"); return nil },
		Signal: func(pid int, sig syscall.Signal) error {
			if pid != -99 || sig != syscall.SIGSTOP {
				t.Fatal("wrong signal")
			}
			steps = append(steps, "signal")
			return nil
		},
		Divert: func(string) error {
			if in.Capsules[1].LeaseHolder != nil || in.Capsules[1].LeaseEpoch <= 4 || in.Capsules[1].Mode != "operator_controlled" {
				t.Fatal("divert before revoke")
			}
			steps = append(steps, "divert")
			return nil
		},
		Act: func(_, act, _ string) error { steps = append(steps, act); return nil },
	}
	if err := a.PbxBarge("agent", 999); err == nil || len(steps) != 0 {
		t.Fatal("wrong extension accepted")
	}
	if err := a.PbxBarge("agent", 321); err != nil {
		t.Fatal(err)
	}
	if strings.Join(steps, ",") != "dispatch,signal,divert" {
		t.Fatal(steps)
	}
	if c.book.FlushedLSN() != c.book.LastLSN() {
		t.Fatal("barge not synced")
	}
	c.config.PBX.Tick = func(int) (uint64, error) { return 124, nil }
	if a.PbxBarge("agent", 321) == nil {
		t.Fatal("recycled PID accepted")
	}
	for _, act := range []string{"open", "claim", "refute", "evidence", "question", "answer", "vote", "handoff", "verdict", "ack"} {
		text := "text"
		if act == "claim" {
			text = "[MEASURED] text"
		}
		if act == "refute" {
			text = "evidence: source"
		}
		if err := a.PbxPublicarActo("agent", act, text); err != nil {
			t.Fatal(act, err)
		}
	}
	if a.PbxPublicarActo("agent", "claim", "bare") == nil || a.PbxPublicarActo("agent", "refute", "bare") == nil {
		t.Fatal("composer guard missing")
	}
}

func TestBindingTierTable(t *testing.T) {
	tiers := map[string]string{
		"ServiciosEstado": "read", "ServiciosAsignar": "H", "ServiciosDesasignar": "H", "ServiciosAprobar": "H", "ServiciosDenegar": "H",
		"EstadoCanonico": "read", "BloqueSlot": "read", "StateURL": "read", "Socket": "read", "CensoMediacion": "read", "CandidatosGrabacion": "read", "EstadoGrabacion": "read", "EstadoPuertas": "read", "GetHostTelemetry": "read", "PbxLeases": "read",
		"CopiarSlot": "U", "AbortarTodo": "U", "FijarDisposicion": "U/L", "CederControl": "H", "TomarControl": "H", "CerrarSlot": "H", "SolicitarArriendo": "H+L", "PbxAccion": "U/H", "PbxBarge": "H", "PbxHangup": "H", "PbxWhisper": "U", "PbxPublicarActo": "U",
		// These legacy methods are owned by WIRE. The table assigns the REQUIRED
		// tier, not evidence that their current implementation enforces it.
		"Enviar": "read/H", "TripGate": "H", "ResetGate": "H", "TripMicroVmGate": "H", "IniciarGrabacion": "H/operator-only", "DetenerGrabacion": "H", "PtyUrl": "L",
		// F1-REC (RAM store): both go through recordingGuard with the slot lease credential (B9/B10).
		"IniciarGrabacionSlot": "H", "DetenerGrabacionSlot": "H",
	}
	ty := reflect.TypeFor[*App]()
	for i := range ty.NumMethod() {
		if tiers[ty.Method(i).Name] == "" {
			t.Errorf("unmapped App binding %s", ty.Method(i).Name)
		}
	}
}

func TestCooperationConcurrentSnapshots(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	c := cooperationFor(a)
	var wg sync.WaitGroup
	for range 4 {
		wg.Go(func() {
			for range 32 {
				if _, err := c.document(); err != nil {
					t.Error(err)
				}
			}
		})
	}
	wg.Go(func() {
		for i := range 32 {
			c.mu.Lock()
			c.cap = i%2 + 1
			c.mu.Unlock()
		}
	})
	wg.Wait()
}

func TestCooperationV2Wall(t *testing.T) {
	a, _, in := cooperationFixture(t)
	cooperationFor(a).config.Grant = func(id, holder string, epoch uint64) error {
		for i := range in.Capsules {
			if cooperationID(in.Capsules[i].Slot) == id {
				in.Capsules[i].Mode = "agent_locked"
				in.Capsules[i].LeaseHolder = &holder
				in.Capsules[i].LeaseEpoch = epoch
				return nil
			}
		}
		return errors.New("unknown")
	}

	in.Cap = 2
	for i := range in.Capsules {
		in.Capsules[i].Mode = "operator_controlled"
	}
	for slot := 3; slot <= 4; slot++ {
		in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: slot, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "operator_controlled"})
	}
	if err := a.FijarDisposicion(1, "2x2", 0, nil); err != nil {
		t.Fatal("wall refused", err)
	}
	d, err := cooperationFor(a).document()
	if err != nil || d.Workspaces[0].Layout != "2x2" {
		t.Fatal("wall not observed", err)
	}
	if err = a.CederControl("srf-ng-4", 0); err != nil {
		t.Fatal(err)
	}
	if _, err = a.SolicitarArriendo("srf-ng-4", "other", 0); err != nil {
		t.Fatal(err)
	}
	if err = a.FijarDisposicion(1, "2x2", 0, nil); err == nil {
		t.Fatal("operator bypassed fourth capsule lease")
	}
}

func TestCooperationWallLeases(t *testing.T) {
	for _, peer := range []bool{false, true} {
		t.Run(map[bool]string{false: "all-held", true: "fourth-held-by-peer"}[peer], func(t *testing.T) {
			a, _, in := cooperationFixture(t)
			for i := range in.Capsules {
				in.Capsules[i].Mode = "operator_controlled"
			}
			for slot := 3; slot <= 4; slot++ {
				in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: slot, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "operator_controlled"})
			}
			cooperationFor(a).config.Grant = func(id, holder string, epoch uint64) error {
				for i := range in.Capsules {
					if cooperationID(in.Capsules[i].Slot) == id {
						in.Capsules[i].Mode = "agent_locked"
						in.Capsules[i].LeaseHolder = &holder
						in.Capsules[i].LeaseEpoch = epoch
						return nil
					}
				}
				return errors.New("unknown")
			}
			var primary LeaseCredential
			for slot := 1; slot <= 4; slot++ {
				id := cooperationID(slot)
				holder := "owner"
				if slot == 4 && peer {
					holder = "peer"
				}
				if err := a.CederControl(id, 0); err != nil {
					t.Fatal(err)
				}
				cred, err := a.SolicitarArriendo(id, holder, 0)
				if err != nil {
					t.Fatal(err)
				}
				if slot == 1 {
					primary = cred
				}
			}
			err := a.FijarDisposicion(1, "2x2", primary.Epoch, &primary)
			if (err != nil) != peer {
				t.Fatalf("peer=%v error=%v", peer, err)
			}
			if peer && !strings.Contains(err.Error(), "LEASE_HELD") {
				t.Fatal("wrong refusal", err)
			}
		})
	}
}
