//go:build cockpit

package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestWireFijarDisposicionQ7Guard(t *testing.T) {
	for _, tc := range []string{"operator-gate", "read-only", "held"} {
		t.Run(tc, func(t *testing.T) {
			a, _, in := cooperationFixture(t)
			c := cooperationFor(a)
			for i := range in.Capsules {
				in.Capsules[i].Mode = "operator_controlled"
			}
			if tc == "operator-gate" {
				c.config.Operator = func(string, string, uint64) bool { return false }
			}
			if tc == "read-only" {
				in.Capsules[1].Mode = "read_only"
			}
			if tc == "held" {
				h := "other"
				in.Capsules[1].Mode = "agent_locked"
				in.Capsules[1].LeaseHolder = &h
				in.Capsules[1].LeaseEpoch = 1
			}
			if err := a.FijarDisposicion(1, "1x2", 0, nil); err == nil {
				t.Fatal("unauthorized layout accepted")
			}
		})
	}
}
func TestWireAgentCallableBindingsGuarded(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	cooperationFor(a).config.Operator = func(string, string, uint64) bool { return false }
	for _, tc := range []struct {
		name string
		call func() error
	}{
		{"monitor", func() error { _, err := a.PbxAccion("monitor", "agent"); return err }},
		{"whisper", func() error { return a.PbxWhisper("agent", "hello") }},
		{"act", func() error { return a.PbxPublicarActo("agent", "ack", "hello") }},
		{"trip", func() error { return a.TripGate("A", "test") }},
		{"microvm", func() error { return a.TripMicroVmGate("A", "test") }},
		{"reset", func() error { return a.ResetGate("A") }},
		{"pty", func() error { _, err := a.PtyUrl("agent"); return err }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := tc.call(); err == nil || !strings.Contains(err.Error(), "OPERATOR-AUTHORITY") {
				t.Fatalf("guard bypass: %v", err)
			}
		})
	}
}
func TestWireEnviarRefusesMutations(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	for _, command := range []string{"detener", "arbitrary"} {
		if _, err := a.Enviar(command, "desktop", 1); err == nil || !strings.Contains(err.Error(), "REFUSED:") {
			t.Fatalf("%s bypass: %v", command, err)
		}
	}
}
func TestWireStartupOrder(t *testing.T) {
	b, err := os.ReadFile("app.go")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	start := strings.Index(s, "func (a *App) startup(")
	end := strings.Index(s, "func (a *App) shutdown(")
	startup := s[start:end]
	last := -1
	for _, hook := range []string{"StartState(", "StartCooperation(", "StartRecording(", "reaper.NewProcessReaper("} {
		p := strings.Index(startup, hook)
		if p <= last {
			t.Fatalf("missing/out-of-order startup %s", hook)
		}
		last = p
	}
	shutdown := s[end:strings.Index(s, "// PtyUrl")]
	last = -1
	for _, hook := range []string{"a.grab.Abortar(", "StopCooperation(", "StopState(", "a.ptys.Cerrar("} {
		p := strings.Index(shutdown, hook)
		if p <= last {
			t.Fatalf("missing/out-of-order shutdown %s", hook)
		}
		last = p
	}
}
func TestWireMainExitCodes(t *testing.T) {
	b, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	if strings.Contains(s, "os.Exit(1)") || !strings.Contains(s, "Menu:") || !strings.Contains(s, "recMenu(app)") {
		t.Fatal("missing REC menu or invalid exit code")
	}
}

func TestBindingTable(t *testing.T) {
	tiers := map[string]string{
		"ServiciosEstado": "read", "ServiciosAsignar": "operator", "ServiciosDesasignar": "operator", "ServiciosAprobar": "operator", "ServiciosDenegar": "operator",
		"EstadoCanonico": "read", "BloqueSlot": "read", "StateURL": "read", "Socket": "read", "CensoMediacion": "read", "CandidatosGrabacion": "read", "EstadoGrabacion": "read", "EstadoPuertas": "read", "GetHostTelemetry": "read", "PbxLeases": "read",
		"CopiarSlot": "slot-lease", "AbortarTodo": "open+journal", "FijarDisposicion": "all-affected-leases/operator", "CederControl": "operator", "TomarControl": "operator", "CerrarSlot": "slot-lease+operator", "SolicitarArriendo": "selected-slot",
		"PbxAccion": "operator", "PbxBarge": "operator", "PbxHangup": "operator", "PbxWhisper": "operator", "PbxPublicarActo": "operator", "Enviar": "read/launch-holder", "TripGate": "operator", "ResetGate": "operator", "TripMicroVmGate": "operator", "PtyUrl": "operator",
		"IniciarGrabacion": "RAM/operator", "DetenerGrabacion": "RAM/operator", "IniciarGrabacionSlot": "RAM/slot-lease", "DetenerGrabacionSlot": "RAM/slot-lease",
	}
	ty := reflect.TypeFor[*App]()
	if ty.NumMethod() != len(tiers) {
		t.Fatalf("binding surface changed: %d != %d", ty.NumMethod(), len(tiers))
	}
	for i := range ty.NumMethod() {
		name := ty.Method(i).Name
		if tiers[name] == "" || strings.HasPrefix(name, "Guardar") || strings.HasPrefix(name, "Save") {
			t.Errorf("unclassified/forbidden binding %s", name)
		}
	}
}

func wireGrant(t *testing.T, a *App, in *stateInput, id, holder string) LeaseCredential {
	t.Helper()
	c := cooperationFor(a)
	c.config.Grant = func(id, holder string, epoch uint64) error {
		for i := range in.Capsules {
			v := &in.Capsules[i]
			if cooperationID(v.Slot) == id {
				v.Mode = "agent_locked"
				v.LeaseHolder = &holder
				v.LeaseEpoch = epoch
				return nil
			}
		}
		return errors.New("missing slot")
	}
	var epoch uint64
	for _, v := range in.Capsules {
		if cooperationID(v.Slot) == id {
			epoch = v.LeaseEpoch
		}
	}
	if err := a.CederControl(id, epoch); err != nil {
		t.Fatal(err)
	}
	cred, err := a.SolicitarArriendo(id, holder, epoch)
	if err != nil {
		t.Fatal(err)
	}
	return cred
}
func TestWireLeaseIsolationAndClose(t *testing.T) {
	a, _, in := cooperationFixture(t)
	cred := wireGrant(t, a, in, "srf-ng-1", "agent-a")
	if _, err := a.CopiarSlot("srf-ng-1", "uri"); err == nil {
		t.Fatal("untokened held copy")
	}
	wrong := cred
	wrong.Holder = "agent-b"
	if _, err := a.CopiarSlot("srf-ng-1", CopyRequest{Kind: "uri", Credential: &wrong}); err == nil {
		t.Fatal("another AI copied held slot")
	}
	if _, err := a.CopiarSlot("srf-ng-1", CopyRequest{Kind: "uri", Credential: &cred}); err != nil {
		t.Fatal(err)
	}
	if err := a.CerrarSlot("srf-ng-1", SlotAuthority{Epoch: cred.Epoch, Credential: &wrong}); err == nil {
		t.Fatal("another AI closed held slot")
	}
	if err := a.CerrarSlot("srf-ng-1", SlotAuthority{Epoch: cred.Epoch, Credential: &cred}); err != nil {
		t.Fatal(err)
	}
	if err := cooperationFor(a).authenticate(StateCapsule{SlotID: "srf-ng-1", Slot: 1, Mode: "agent_locked", LeaseHolder: &cred.Holder, LeaseEpoch: cred.Epoch}, &cred); err == nil {
		t.Fatal("closed slot token survived")
	}
}
func TestWireLayoutCrossWorkspaceLeases(t *testing.T) {
	a, _, in := cooperationFixture(t)
	in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: 3, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "operator_controlled"})
	primary := wireGrant(t, a, in, "srf-ng-1", "agent-a")
	other := wireGrant(t, a, in, "srf-ng-3", "agent-b")
	in.Capsules[1].Mode = "operator_controlled"
	if err := a.FijarDisposicion(1, "1x1", primary.Epoch, &primary); err == nil || !strings.Contains(err.Error(), "LEASE_HELD") {
		t.Fatal("global cap escaped ws1", err)
	}
	if err := a.TomarControl("srf-ng-3", other.Epoch); err != nil {
		t.Fatal(err)
	}
	if err := a.FijarDisposicion(1, "1x2", primary.Epoch, &primary); err != nil {
		t.Fatal("unrelated Op epoch rejected", err)
	}
	if err := a.FijarDisposicion(1, "1x1", primary.Epoch, nil); err == nil {
		t.Fatal("untokened held layout")
	}
}
func TestWireOperatorLayoutUsesSequence(t *testing.T) {
	a, _, in := cooperationFixture(t)
	for i := range in.Capsules {
		in.Capsules[i].Mode = "operator_controlled"
		in.Capsules[i].LeaseEpoch = uint64(i + 4)
	}
	d, err := cooperationFor(a).document()
	if err != nil {
		t.Fatal(err)
	}
	var called bool
	cooperationFor(a).config.Operator = func(kind, target string, epoch uint64) bool {
		called = true
		return kind == "layout" && target == "ws-1" && epoch == uint64(d.Seq)
	}
	if err := a.FijarDisposicion(1, "1x2", 999, nil); err != nil || !called {
		t.Fatal("compared distinct slot epochs", err)
	}
}
func TestWireProductionOperatorGateAllowsAndJournals(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	native := nativeCooperationConfig(a)
	cooperationFor(a).config.Operator = native.Operator
	c := cooperationFor(a)
	before := c.book.LastLSN()
	if err := AuthorizeCooperationOperator(a, "takeover", "srf-ng-1", 23); err != nil {
		t.Fatal(err)
	}
	if c.book.LastLSN() <= before || c.book.FlushedLSN() != c.book.LastLSN() {
		t.Fatal("operator action not durably journaled")
	}
	injected := NewApp(WithOperatorGate(func(string, string, uint64) bool { return false }))
	if nativeCooperationConfig(injected).Operator("trip", "A", 0) {
		t.Fatal("fake gate ignored")
	}
}
func TestWireLaunchHolderAndCustody(t *testing.T) {
	a, _, in := cooperationFixture(t)
	_ = wireGrant(t, a, in, "srf-ng-1", "existing") // installs fake native Grant
	c := cooperationFor(a)
	sends := 0
	send := func(command, clase string, slot uint16) (*RespuestaSrfd, error) {
		sends++
		in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: int(slot), Archetype: clase, Lifecycle: "ready", Health: "healthy", Mode: "operator_controlled"})
		if a.EstadoCanonico() != "" {
			t.Error("launch published before custody")
		}
		return &RespuestaSrfd{OK: true}, nil
	}
	if _, err := launchGuard(a, "desktop", 3, "", send); err == nil || sends != 0 {
		t.Fatal("launch without holder")
	}
	got, err := launchGuard(a, "desktop", 3, "launching-ai", send)
	if err != nil || got.Lease == nil || sends != 1 {
		t.Fatal("free slot launch refused", err)
	}
	s, err := c.slot("srf-ng-3", got.Lease.Epoch)
	if err != nil || s.Mode != "agent_locked" || s.LeaseHolder == nil || *s.LeaseHolder != "launching-ai" {
		t.Fatal("launch custody missing", err)
	}
	if err = c.authenticate(s, got.Lease); err != nil {
		t.Fatal(err)
	}
	wrong := *got.Lease
	wrong.Holder = "other-ai"
	if c.authenticate(s, &wrong) == nil {
		t.Fatal("another AI drove launched slot")
	}
	if _, err = launchGuard(a, "desktop", 3, "launching-ai", send); err == nil || sends != 1 {
		t.Fatal("occupied slot launched")
	}
	c.operation.Lock()
	_, err = launchGuard(a, "desktop", 4, "launching-ai", send)
	c.operation.Unlock()
	if err == nil || sends != 1 {
		t.Fatal("concurrent launch admitted")
	}
}
func TestWireLaunchMissingActuatorsStopsBeforeSend(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	sent := false
	_, err := launchGuard(a, "desktop", 3, "agent", func(string, string, uint16) (*RespuestaSrfd, error) {
		sent = true
		return &RespuestaSrfd{OK: true}, nil
	})
	if err == nil || sent {
		t.Fatal("unowned production launch")
	}
}

type fakeD11 struct {
	p                            CaptureProvenance
	held                         FrameDigest
	framebuffer                  FrameDigest
	reaped, starts, stops, reads int
	frozen                       bool
	lieProbe                     bool
	downgradeSize, downgradeStop bool
}

func newFakeD11() *fakeD11 {
	bytes := []byte{0, 0, 0, 255, 255, 0, 0, 255}
	f := FrameDigest{Seq: 1, W: 2, H: 1, Stride: 8, Format: "BGRA", SHA256: sha256.Sum256(bytes)}
	return &fakeD11{p: CaptureProvenance{Source: "x11-framebuffer", Codec: "raw", Store: "vadapt.ramws", XID: 42}, held: f, framebuffer: f}
}
func (f *fakeD11) Start(context.Context, int, uint32, int64) (string, error) {
	f.starts++
	if !f.frozen {
		return "", errors.New("not frozen")
	}
	return "ram-frame", nil
}
func (f *fakeD11) Stop(context.Context, string) error {
	f.stops++
	if f.downgradeStop {
		f.p.Codec = "h264"
	}
	return nil
}
func (f *fakeD11) Reap(context.Context, string) error { f.reaped++; return nil }
func (f *fakeD11) SizeBytes(context.Context, string) (int64, error) {
	if f.downgradeSize {
		f.p.Codec = "h264"
	}
	return 8, nil
}
func (f *fakeD11) Provenance(context.Context, string) (CaptureProvenance, error) { return f.p, nil }
func (f *fakeD11) Probe(context.Context, string, string, uint32) (FrameDigest, error) {
	if f.lieProbe {
		return f.framebuffer, nil
	}
	return f.held, nil
}
func (f *fakeD11) Read(context.Context, string, uint32) (FrameDigest, error) {
	f.reads++
	if !f.frozen {
		return FrameDigest{}, errors.New("unfrozen framebuffer")
	}
	return f.framebuffer, nil
}
func (f *fakeD11) Freeze(context.Context, string, uint32) (func() error, error) {
	if f.frozen {
		return nil, errors.New("nested freeze")
	}
	f.frozen = true
	return func() error { f.frozen = false; return nil }, nil
}
func TestWireAdmitRamCaptureLiveSessionAndBitExact(t *testing.T) {
	ctx := context.Background()
	t.Run("frozen stored frame", func(t *testing.T) {
		f := newFakeD11()
		r := admitRamCapture(f, f, ":calibration")
		id, err := r.Start(ctx, 1, 42, 5000)
		if err != nil || id == "" || f.frozen {
			t.Fatal("admission", err)
		}
		if size, err := r.SizeBytes(ctx, id); err != nil || size != 8 {
			t.Fatal("size", err)
		}
		if err = r.Stop(ctx, id); err != nil {
			t.Fatal(err)
		}
		if f.stops != 1 || f.reaped != 0 || f.reads < 3 {
			t.Fatal("verification not performed")
		}
		if err = r.Reap(ctx, id); err != nil {
			t.Fatal(err)
		}
		if _, err = r.SizeBytes(ctx, id); err == nil {
			t.Fatal("reaped session retained")
		}
	})
	for _, tc := range []struct {
		name   string
		mutate func(*fakeD11)
	}{
		{"xpra", func(f *fakeD11) { f.p.Source = "xpra" }},
		{"h264", func(f *fakeD11) { f.p.Codec = "h264" }},
		{"disk store", func(f *fakeD11) { f.p.Store = "disk" }},
		{"swap", func(f *fakeD11) { f.p.Swap = true }},
		{"disk", func(f *fakeD11) { f.p.Disk = true }},
		{"zero xid", func(f *fakeD11) { f.p.XID = 0 }},
		{"wrong xid", func(f *fakeD11) { f.p.XID = 41 }},
		{"sha drift", func(f *fakeD11) { f.held.SHA256[0] ^= 1 }},
		{"bad stride", func(f *fakeD11) { f.held.Stride = 7 }},
		{"empty frame", func(f *fakeD11) { f.held.W = 0 }},
		{"not 32 bpp", func(f *fakeD11) { f.held.Format = "RGB" }},
		{"framebuffer probe but h264 store", func(f *fakeD11) { f.lieProbe = true; f.p.Codec = "h264"; f.held.SHA256[0] ^= 1 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFakeD11()
			tc.mutate(f)
			r := admitRamCapture(f, f, ":calibration")
			id, err := r.Start(ctx, 1, 42, 5000)
			if err == nil || id != "" || f.reaped != 1 || f.frozen {
				t.Fatalf("unsafe capture admitted: error=%v reaps=%d", err, f.reaped)
			}
		})
	}
	for _, operation := range []string{"size", "stop", "during-size", "during-stop"} {
		t.Run(operation+" downgrade", func(t *testing.T) {
			f := newFakeD11()
			r := admitRamCapture(f, f, ":calibration")
			id, err := r.Start(ctx, 1, 42, 5000)
			if err != nil {
				t.Fatal(err)
			}
			if operation == "during-size" {
				f.downgradeSize = true
			} else if operation == "during-stop" {
				f.downgradeStop = true
			} else {
				f.p.Codec = "h264"
			}
			if strings.Contains(operation, "size") {
				_, err = r.SizeBytes(ctx, id)
			} else {
				err = r.Stop(ctx, id)
			}
			if err == nil || f.reaped != 1 || f.frozen {
				t.Fatal("mid-session downgrade accepted", err)
			}
		})
	}
	if admitRamCapture(nil, nil, "") != nil {
		t.Fatal("missing D11 manufactured capture")
	}
}
func TestWireStateSnapshotSamplesCapsuleTelemetry(t *testing.T) {
	// Use the existing fake srfd transport; only real procfs memory is accepted.
	stateReply(t, fmt.Sprintf(`{"ok":true,"rc":0,"datos":[{"slot":5,"clase":"desktop","estado":"listo","pid":%d}]}`, os.Getpid()))
	a := NewApp()
	s := &stateServer{app: a, epoch: time.Now().Add(-time.Second)}
	in, err := s.readInputs()
	if err != nil {
		t.Fatal(err)
	}
	if len(in.Capsules) != 1 || in.Capsules[0].Telemetry.Memory == nil {
		t.Fatal("PID sensor not sampled")
	}
	m := in.Capsules[0].Telemetry.Memory
	if m.Value <= 0 || m.Timestamp < 1 || (m.Source != "procfs" && m.Source != "cgroup") {
		t.Fatal("invented capsule metric")
	}
}

func TestWireWailsBindingArgumentsAreFixed(t *testing.T) {
	ty := reflect.TypeFor[*App]()
	for i := range ty.NumMethod() {
		m := ty.Method(i)
		if m.Type.IsVariadic() {
			t.Errorf("Wails reflect.Call cannot invoke variadic binding %s", m.Name)
		}
	}
}

func TestWireD11StoredSequenceCannotChangeIdentity(t *testing.T) {
	f := newFakeD11()
	r := admitRamCapture(f, f, ":calibration")
	ctx := context.Background()
	id, err := r.Start(ctx, 1, 42, 5000)
	if err != nil {
		t.Fatal(err)
	}
	f.held.SHA256[0] ^= 1
	f.framebuffer = f.held
	if _, err = r.SizeBytes(ctx, id); err == nil || f.reaped != 1 {
		t.Fatal("same stored Seq changed identity")
	}
}
func TestWireD11TypedNilIsAbsent(t *testing.T) {
	var d *fakeD11
	if admitRamCapture(d, newFakeD11(), ":calibration") != nil {
		t.Fatal("typed nil D11 admitted")
	}
	if admitRamCapture(newFakeD11(), d, ":calibration") != nil {
		t.Fatal("typed nil framebuffer admitted")
	}
}

func TestWireFixedArityDTOTransport(t *testing.T) {
	a, _, in := cooperationFixture(t)
	cred := wireGrant(t, a, in, "srf-ng-1", "agent-a")
	// Wails decodes interface{} request objects from JSON, then uses reflect.Call.
	var request any
	raw, err := json.Marshal(CopyRequest{Kind: "uri", Credential: &cred})
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(raw, &request); err != nil {
		t.Fatal(err)
	}
	result := reflect.ValueOf(a).MethodByName("CopiarSlot").Call([]reflect.Value{reflect.ValueOf("srf-ng-1"), reflect.ValueOf(request)})
	if !result[1].IsNil() {
		t.Fatal("Wails-compatible invocation refused")
	}
	copied := result[0].Interface().(CopyResult)
	if !strings.HasSuffix(copied.Bytes, "/slot/srf-ng-1") {
		t.Fatal("wrong copy target")
	}
	raw, err = json.Marshal(SlotAuthority{Epoch: cred.Epoch, Credential: &cred})
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(raw, &request); err != nil {
		t.Fatal(err)
	}
	result = reflect.ValueOf(a).MethodByName("CerrarSlot").Call([]reflect.Value{reflect.ValueOf("srf-ng-1"), reflect.ValueOf(request)})
	if !result[0].IsNil() {
		t.Fatal("Wails-compatible close refused")
	}
	target, err := decodeLaunchRequest(map[string]any{"slot": float64(3), "holder": "agent-a"})
	if err != nil || target.Slot != 3 || target.Holder != "agent-a" {
		t.Fatal("launch transport lost holder")
	}
	for _, bad := range []any{nil, -1, 1.5, map[string]any{"holder": "agent"}, map[string]any{"slot": nil}, map[string]any{"slot": 3, "extra": true}} {
		if _, err := decodeLaunchRequest(bad); err == nil {
			t.Fatal("invalid launch DTO accepted")
		}
	}
}
func TestWireLegacyRecBindingsGuardedAndRAMOnly(t *testing.T) {
	a, _, _ := cooperationFixture(t)
	c := cooperationFor(a)
	if err := StartRecording(a, RecordingConfig{}); err != nil {
		t.Fatal(err)
	}
	c.config.Operator = func(string, string, uint64) bool { return false }
	if _, err := a.IniciarGrabacion(1, 0); err == nil || !strings.Contains(err.Error(), "OPERATOR-AUTHORITY") {
		t.Fatal("legacy REC unguarded", err)
	}
	if rs := a.EstadoGrabacion(); rs.Store != nil || rs.Fase == "recording" {
		t.Fatal("legacy REC created storage")
	}
	c.config.Operator = nativeCooperationConfig(a).Operator
	if _, err := a.IniciarGrabacion(1, 0); err == nil || !strings.Contains(err.Error(), "AUSENTE: RAM capture") {
		t.Fatal("missing D11 fell back to disk", err)
	}
}

func TestWireNativeCustodyHooksPresent(t *testing.T) {
	cfg := nativeCooperationConfig(NewApp())
	if cfg.SetMode == nil || cfg.Grant == nil || cfg.Revoke == nil {
		t.Fatal("native cockpit custody hooks missing")
	}
}
