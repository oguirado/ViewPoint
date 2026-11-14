//go:build cockpit

package main

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestRecordingPolicyBeforeInstall(t *testing.T) {
	a := NewApp()
	a.grab.raiz = t.TempDir()
	// No process may be started, even with the old Wails binding.
	_, err := a.IniciarGrabacion(1, 0)
	if err == nil || !strings.Contains(err.Error(), "AUSENTE: recording policy") {
		t.Fatalf("uninstalled policy: %v", err)
	}
}

type recordingFake struct {
	startErr             error
	starts, stops, reaps int
	ttl                  int64
}

func (f *recordingFake) Start(ctx context.Context, slot int, xid uint32, ttl int64) (string, error) {
	f.starts++
	f.ttl = ttl
	return "ram-session", f.startErr
}
func (f *recordingFake) Stop(context.Context, string) error               { f.stops++; return nil }
func (f *recordingFake) Reap(context.Context, string) error               { f.reaps++; return nil }
func (f *recordingFake) SizeBytes(context.Context, string) (int64, error) { return 64, nil }

type recordingExporter func(context.Context, string, string) (string, error)

func (f recordingExporter) Export(ctx context.Context, id, dir string) (string, error) {
	return f(ctx, id, dir)
}
func recordingSetup(t *testing.T, ttl int64) (*App, *recordingFake, *stateInput) {
	t.Helper()
	a, _, in := cooperationFixture(t)
	a.grab.raiz = t.TempDir()
	fake := &recordingFake{}
	if err := StartRecording(a, RecordingConfig{RamCapture: fake, TTLms: ttl}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.grab.Abortar)
	return a, fake, in
}
func recordingStart(t *testing.T, a *App) string {
	t.Helper()
	id, err := a.IniciarGrabacionSlot("srf-ng-1", 42, 0, nil)
	if err != nil {
		t.Fatal(err)
	}
	return id
}
func recordingRequire(t *testing.T, err error, literal string) {
	t.Helper()
	if err == nil || !strings.Contains(err.Error(), literal) {
		t.Fatalf("wanted %q; got %v", literal, err)
	}
}
func recordingNoFiles(t *testing.T, a *App) {
	t.Helper()
	files, err := os.ReadDir(a.grab.raiz)
	if err != nil || len(files) != 0 {
		t.Fatalf("recording wrote disk: %v %v", files, err)
	}
}
func TestRecordingPolicyRAMAndCanonical(t *testing.T) {
	a, f, _ := recordingSetup(t, 0)
	id := recordingStart(t, a)
	rs := a.EstadoGrabacion()
	if rs.Dir != "" || rs.Store == nil || *rs.Store != "ram" || rs.TTL == nil || *rs.TTL != 1800000 || f.ttl != 1800000 {
		t.Fatalf("RAM metadata: %+v", rs)
	}
	var first, second StateDocument
	if err := json.Unmarshal([]byte(a.EstadoCanonico()), &first); err != nil {
		t.Fatal(err)
	}
	time.Sleep(25 * time.Millisecond)
	if err := json.Unmarshal([]byte(a.EstadoCanonico()), &second); err != nil {
		t.Fatal(err)
	}
	copy, err := a.CopiarSlot("srf-ng-1", "bloque")
	if err != nil {
		t.Fatal(err)
	}
	if first.Seq != second.Seq || copy.Seq != first.Seq || copy.Bytes != a.BloqueSlot("srf-ng-1") {
		t.Fatal("volatile recording projection")
	}
	if second.Capsules[0].Recording.Store == nil || *second.Capsules[0].Recording.Store != "ram" || second.Capsules[1].Recording.Phase != "idle" {
		t.Fatalf("wrong slot projection: %+v", second.Capsules)
	}
	if err = a.DetenerGrabacionSlot("srf-ng-1", id, 0, nil); err != nil {
		t.Fatal(err)
	}
	sealed := a.EstadoGrabacion()
	time.Sleep(15 * time.Millisecond)
	later := a.EstadoGrabacion()
	if sealed.Fase != "sealed" || sealed.Dir != "" || *sealed.Store != "ram" || later.SinceMonoMs <= sealed.SinceMonoMs || later.DuracionMs != sealed.DuracionMs {
		t.Fatalf("seal duration/TTL: %+v -> %+v", sealed, later)
	}
	if a.EstadoCanonico() == "" {
		t.Fatal("invalid sealed RAM state")
	}
	recordingNoFiles(t, a)
	if err = StartRecording(a, RecordingConfig{}); err == nil {
		t.Fatal("double source wrapper")
	}
}
func TestRecordingPolicyGuardsAndLegacy(t *testing.T) {
	a, f, in := recordingSetup(t, 0)
	_, err := a.IniciarGrabacionSlot("srf-ng-1", 0, 1, nil)
	recordingRequire(t, err, "STALE-EPOCH")
	_, err = a.IniciarGrabacionSlot("srf-ng-9", 0, 0, nil)
	recordingRequire(t, err, "UNKNOWN-SLOT")
	_, err = a.IniciarGrabacionSlot("srf-ng-1", 0, 0, &LeaseCredential{})
	recordingRequire(t, err, "OPERATOR-CONTROL")
	_, err = a.IniciarGrabacionSlot("srf-ng-2", 0, 0, nil)
	recordingRequire(t, err, "LEASE-REQUIRED")
	cooperationFor(a).config.Operator = nil
	_, err = a.IniciarGrabacion(1, 0)
	recordingRequire(t, err, "OPERATOR-AUTHORITY")
	cooperationFor(a).config.Operator = func(string, string, uint64) bool { return false }
	_, err = a.IniciarGrabacion(1, 0)
	recordingRequire(t, err, "OPERATOR-AUTHORITY")
	cooperationFor(a).config.Operator = func(string, string, uint64) bool { return true }
	holder := "agent"
	in.Capsules[0].Mode = "agent_locked"
	in.Capsules[0].LeaseHolder = &holder
	in.Capsules[0].LeaseEpoch = 1
	_, err = a.IniciarGrabacion(1, 0)
	recordingRequire(t, err, "LEASE-REQUIRED")
	_, err = a.IniciarGrabacionSlot("srf-ng-1", 0, 1, &LeaseCredential{Holder: "agent", Epoch: 1, Token: strings.Repeat("0", 32)})
	recordingRequire(t, err, "LEASE-REQUIRED")
	if f.starts != 0 {
		t.Fatal("refused request reached backend")
	}
	in.Capsules[0].Mode = "operator_controlled"
	in.Capsules[0].LeaseHolder = nil
	in.Capsules[0].LeaseEpoch = 0
	id, err := a.IniciarGrabacion(1, 0)
	if err != nil {
		t.Fatal(err)
	}
	in.Capsules[0].Mode = "agent_locked"
	in.Capsules[0].LeaseHolder = &holder
	in.Capsules[0].LeaseEpoch = 1
	recordingRequire(t, a.DetenerGrabacion(id), "LEASE-REQUIRED")
	in.Capsules[0].Mode = "operator_controlled"
	in.Capsules[0].LeaseHolder = nil
	in.Capsules[0].LeaseEpoch = 0
	recordingRequire(t, a.DetenerGrabacionSlot("srf-ng-2", id, 0, nil), "LEASE-REQUIRED")
	recordingRequire(t, a.DetenerGrabacionSlot("srf-ng-1", "wrong", 0, nil), "session mismatch")
	if err = a.DetenerGrabacion(id); err != nil {
		t.Fatal(err)
	}
	recordingNoFiles(t, a)
}
func TestRecordingPolicyLeaseCanRecordRAM(t *testing.T) {
	a, _, in := recordingSetup(t, 0)
	cooperationFor(a).config.Grant = func(id, holder string, epoch uint64) error {
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
	if err := a.CederControl("srf-ng-1", 0); err != nil {
		t.Fatal(err)
	}
	d, err := cooperationFor(a).document()
	if err != nil {
		t.Fatal(err)
	}
	cred, err := a.SolicitarArriendo("srf-ng-1", "agent", d.Capsules[0].LeaseEpoch)
	if err != nil {
		t.Fatal(err)
	}
	id, err := a.IniciarGrabacionSlot("srf-ng-1", 0, cred.Epoch, &cred)
	if err != nil {
		t.Fatal(err)
	}
	if err = a.DetenerGrabacionSlot("srf-ng-1", id, cred.Epoch, &cred); err != nil {
		t.Fatal(err)
	}
	recordingRequire(t, triggerOperatorSave(a), "OPERATOR-AUTHORITY")
	recordingNoFiles(t, a)
}
func TestRecordingPolicyExpiryAndAbort(t *testing.T) {
	a, f, _ := recordingSetup(t, 80)
	recordingStart(t, a)
	// Compose the actual AbortarTodo actuator exactly as WIRE must.
	c := cooperationFor(a)
	previous := c.config.Stop
	c.config.Stop = func(id string) error {
		if err := stopRecordingForAbort(a, id); err != nil {
			return err
		}
		return previous(id)
	}
	if err := a.AbortarTodo(); err != nil {
		t.Fatal(err)
	}
	rs := a.EstadoGrabacion()
	if rs.Fase != "sealed" || rs.Store == nil || *rs.Store != "ram" || f.stops != 1 {
		t.Fatalf("abort did not seal: %+v", rs)
	}
	recordingNoFiles(t, a)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if a.EstadoGrabacion().Fase == "idle" {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	rs = a.EstadoGrabacion()
	if rs.Fase != "idle" || rs.Store != nil || rs.TTL != nil {
		t.Fatalf("expiry: %+v", rs)
	}
	recordingNoFiles(t, a)
}
func TestRecordingPolicyMissingAndLiteral(t *testing.T) {
	a, _, _ := recordingSetup(t, 0)
	a.grab.policy.config.RamCapture = nil
	_, err := a.IniciarGrabacion(1, 0)
	recordingRequire(t, err, "AUSENTE: RAM capture")
	recordingNoFiles(t, a)
	a.grab.policy.config.RamCapture = &recordingFake{startErr: errors.New("grabador_objetivo_ambiguo /home/private/source")}
	_, err = a.IniciarGrabacion(1, 0)
	recordingRequire(t, err, "grabador_objetivo_ambiguo")
	rs := a.EstadoGrabacion()
	if strings.Contains(rs.Error, "/home") || strings.Contains(a.grab.err, "/home") {
		t.Fatal("private path in literal")
	}
}
func TestRecordingPolicyOperatorSave(t *testing.T) {
	for _, kind := range []string{"nil-operator", "nil-dialog", "nil-exporter", "cancel", "partial", "symlink", "complete"} {
		t.Run(kind, func(t *testing.T) {
			a, _, _ := recordingSetup(t, 0)
			id := recordingStart(t, a)
			if err := a.DetenerGrabacion(id); err != nil {
				t.Fatal(err)
			}
			p := a.grab.policy
			p.config.Exporter = recordingExporter(func(_ context.Context, _ string, root string) (string, error) {
				names := []string{"video-000.mp4", "eventos.jsonl", "FLUJO.md", "manifiesto.json"}
				if kind == "partial" {
					names = names[:3]
				}
				for _, name := range names {
					if err := os.WriteFile(filepath.Join(root, name), []byte("verified test artifact"), 0600); err != nil {
						return "", err
					}
				}
				if kind == "symlink" {
					if err := os.Remove(filepath.Join(root, "video-000.mp4")); err != nil {
						return "", err
					}
					if err := os.Symlink(filepath.Join(root, "FLUJO.md"), filepath.Join(root, "video-000.mp4")); err != nil {
						return "", err
					}
				}
				return root, nil
			})
			p.config.Dialog = func(req SaveDialogRequest) (SaveDialogChoice, error) {
				if req.SizeBytes <= 0 || req.TargetDir != a.grab.raiz || req.Title != "Guardar grabación srf-ng-1" || len(req.Buttons) != 3 {
					t.Fatalf("untruthful preview: %+v", req)
				}
				if kind == "cancel" {
					return SaveDialogChoice{Action: "Cancelar"}, nil
				}
				return SaveDialogChoice{Action: "Guardar", TargetDir: req.TargetDir}, nil
			}
			switch kind {
			case "nil-operator":
				cooperationFor(a).config.Operator = nil
			case "nil-dialog":
				p.config.Dialog = nil
			case "nil-exporter":
				p.config.Exporter = nil
			}
			err := triggerOperatorSave(a)
			rs := a.EstadoGrabacion()
			switch kind {
			case "complete":
				if err != nil || rs.Store == nil || *rs.Store != "disk" || rs.TTL != nil || rs.Dir != a.grab.raiz || a.EstadoCanonico() == "" {
					t.Fatalf("save: %+v %v", rs, err)
				}
			case "cancel":
				if err != nil {
					t.Fatal(err)
				}
				recordingNoFiles(t, a)
			default:
				if err == nil || rs.Store == nil || *rs.Store != "ram" {
					t.Fatalf("unsafe save (%s): %+v %v", kind, rs, err)
				}
				if strings.HasPrefix(kind, "nil-") {
					recordingNoFiles(t, a)
				}
			}
		})
	}
	typ := reflect.TypeFor[*App]()
	for i := range typ.NumMethod() {
		name := typ.Method(i).Name
		for _, prefix := range []string{"Guardar", "Save", "Exportar", "Disco"} {
			if strings.HasPrefix(name, prefix) {
				t.Fatalf("save binding %s", name)
			}
		}
	}
}
func TestRecordingPolicyNativeMenu(t *testing.T) {
	a := NewApp()
	m := recMenu(a)
	if len(m.Items) != 1 || !m.Items[0].Disabled || m.Items[0].Label != "Guardar grabación en disco…" {
		t.Fatalf("menu: %+v", m)
	}
	// The menu is normally created before startup installs the policy.
	b, _, _ := recordingSetup(t, 0)
	b.grab.nativeMenu = m.Items[0]
	id := recordingStart(t, b)
	if !m.Items[0].Disabled {
		t.Fatal("save enabled while live")
	}
	if err := b.DetenerGrabacion(id); err != nil {
		t.Fatal(err)
	}
	if m.Items[0].Disabled {
		t.Fatal("save not enabled after RAM seal")
	}
}

type recordingHTTPListener struct{ net.Listener }

func (recordingHTTPListener) Addr() net.Addr {
	return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 32100}
}
func TestRecordingPolicyHTTPNeverSaves(t *testing.T) {
	a, _, _ := recordingSetup(t, 0)
	id := recordingStart(t, a)
	if err := a.DetenerGrabacion(id); err != nil {
		t.Fatal(err)
	}
	p := a.grab.policy
	p.config.Dialog = func(SaveDialogRequest) (SaveDialogChoice, error) {
		t.Fatal("HTTP reached native dialog")
		return SaveDialogChoice{}, nil
	}
	p.config.Exporter = recordingExporter(func(context.Context, string, string) (string, error) {
		t.Fatal("HTTP reached exporter")
		return "", nil
	})
	s := cooperationFor(a).server
	s.listener = recordingHTTPListener{}
	s.credential = "unit-test-only"
	for _, method := range []string{"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} {
		for _, path := range []string{"/state", "/save", "/state/save", "/recording/save"} {
			req := httptest.NewRequest(method, "http://127.0.0.1:32100"+path, nil)
			req.Header.Set("Authorization", "Bearer unit-test-only")
			res := httptest.NewRecorder()
			s.ServeHTTP(res, req)
			expected := 404
			if path == "/state" {
				expected = 405
				if method == "GET" || method == "HEAD" {
					expected = 200
				}
			}
			if res.Code != expected {
				t.Fatalf("%s %s: %d", method, path, res.Code)
			}
		}
	}
	recordingNoFiles(t, a)
}

func TestRecordingPolicyDialogDoesNotBlockReapOrAbort(t *testing.T) {
	a, _, _ := recordingSetup(t, 80)
	id := recordingStart(t, a)
	if err := a.DetenerGrabacion(id); err != nil {
		t.Fatal(err)
	}
	p := a.grab.policy
	p.config.Exporter = recordingExporter(func(context.Context, string, string) (string, error) { t.Fatal("expired RAM exported"); return "", nil })
	p.config.Dialog = func(req SaveDialogRequest) (SaveDialogChoice, error) {
		c := cooperationFor(a)
		stop := c.config.Stop
		c.config.Stop = func(id string) error {
			if err := stopRecordingForAbort(a, id); err != nil {
				return err
			}
			return stop(id)
		}
		if err := a.AbortarTodo(); err != nil {
			t.Fatal(err)
		}
		deadline := time.Now().Add(time.Second)
		for time.Now().Before(deadline) {
			if a.EstadoGrabacion().Fase == "idle" {
				break
			}
			time.Sleep(5 * time.Millisecond)
		}
		if a.EstadoGrabacion().Fase != "idle" {
			t.Fatal("dialog prevented TTL reap")
		}
		return SaveDialogChoice{Action: "Guardar", TargetDir: req.TargetDir}, nil
	}
	recordingRequire(t, triggerOperatorSave(a), "RAM expired or changed")
	recordingNoFiles(t, a)
}
