//go:build cockpit

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/browserid/services"
	"opensuperficies.local/srf/src/orchestratorbook/wal"
	"opensuperficies.local/srf/src/surfacebook/manager"
)

func serviciosFixture(t *testing.T) (*App, string, *services.Registry) {
	t.Helper()
	a, _, _ := cooperationFixture(t)
	dir := filepath.Join(t.TempDir(), "services")
	t.Setenv("SRF_SOCKET", filepath.Join(filepath.Dir(dir), "srfd.sock"))
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "catalog.json"), []byte(`["chatgpt","perplexity"]`), 0600); err != nil {
		t.Fatal(err)
	}
	cat, err := loadServiciosCatalog(dir)
	if err != nil {
		t.Fatal(err)
	}
	r, err := services.Open(dir, cat)
	if err != nil {
		t.Fatal(err)
	}
	return a, dir, r
}
func TestServiciosWriteReadJournal(t *testing.T) {
	a, dir, r := serviciosFixture(t)
	if err := a.ServiciosAsignar("srf-ng-1", "chatgpt"); err != nil {
		t.Fatal(err)
	}
	s, err := a.ServiciosEstado()
	if err != nil {
		t.Fatal(err)
	}
	if s.Revision != 1 || !reflect.DeepEqual(s.Services, []string{"chatgpt", "perplexity"}) || !reflect.DeepEqual(s.Assignments["srf-ng-1"], []string{"chatgpt"}) || !reflect.DeepEqual(s.Capsules, []string{"srf-ng-1", "srf-ng-2"}) {
		t.Fatalf("state: %+v", s)
	}
	if err := a.ServiciosDesasignar("srf-ng-1", "chatgpt"); err != nil {
		t.Fatal(err)
	}
	yes, err := r.RequestAccess("ai-a", "capsule-3", "perplexity", "research")
	if err != nil {
		t.Fatal(err)
	}
	no, err := r.RequestAccess("ai-a", "srf-ng-2", "chatgpt", "research")
	if err != nil {
		t.Fatal(err)
	}
	s, err = a.ServiciosEstado()
	if err != nil || len(s.Pending) != 2 {
		t.Fatalf("pending: %+v %v", s, err)
	}
	if err := a.ServiciosAprobar(yes.ID); err != nil {
		t.Fatal(err)
	}
	if err := a.ServiciosDenegar(no.ID); err != nil {
		t.Fatal(err)
	}
	s, err = a.ServiciosEstado()
	if err != nil || len(s.Pending) != 0 || s.Revision != 6 || !reflect.DeepEqual(s.Assignments["capsule-3"], []string{"perplexity"}) {
		t.Fatalf("resolved: %+v %v", s, err)
	}
	b, err := os.ReadFile(filepath.Join(dir, "journal.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) != 4 {
		t.Fatalf("journal %s", b)
	}
	for i, action := range []string{"assign", "unassign", "approve", "deny"} {
		var entry services.JournalEntry
		if json.Unmarshal([]byte(lines[i]), &entry) != nil || entry.Action != action || entry.Actor != "operator" {
			t.Fatalf("entry %s", lines[i])
		}
	}
}
func TestServiciosGateRefusal(t *testing.T) {
	a, dir, r := serviciosFixture(t)
	req, err := r.RequestAccess("ai-a", "srf-ng-1", "chatgpt", "research")
	if err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(filepath.Join(dir, "assignments.json"))
	if err != nil {
		t.Fatal(err)
	}
	walPath := filepath.Join(os.Getenv("XDG_RUNTIME_DIR"), "os-cu", "cooperation.wal")
	beforeWal, err := os.ReadFile(walPath)
	if err != nil {
		t.Fatal(err)
	}
	cfg := nativeCooperationConfig(NewApp(WithOperatorGate(func(string, string, uint64) bool { return false })))
	cooperationFor(a).config.Operator = cfg.Operator
	for _, op := range []func() error{func() error { return a.ServiciosAsignar("srf-ng-1", "chatgpt") }, func() error { return a.ServiciosDesasignar("srf-ng-1", "chatgpt") }, func() error { return a.ServiciosAprobar(req.ID) }, func() error { return a.ServiciosDenegar(req.ID) }} {
		if err := op(); err == nil || !strings.Contains(err.Error(), "operator capability") {
			t.Fatalf("refusal: %v", err)
		}
		after, err := os.ReadFile(filepath.Join(dir, "assignments.json"))
		if err != nil || string(before) != string(after) {
			t.Fatal("refusal mutated registry", err)
		}
		afterWal, err := os.ReadFile(walPath)
		if err != nil || string(beforeWal) != string(afterWal) {
			t.Fatal("refusal mutated operator journal", err)
		}
	}
}
func TestServiciosCorruptionAndCatalogFailClosed(t *testing.T) {
	a, dir, _ := serviciosFixture(t)
	if err := os.WriteFile(filepath.Join(dir, "assignments.json"), []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := a.ServiciosEstado(); err == nil {
		t.Fatal("corruption accepted")
	}
	if err := a.ServiciosAsignar("srf-ng-1", "chatgpt"); err == nil {
		t.Fatal("corrupt write accepted")
	}
	for _, data := range []string{`null`, `["chatgpt","chatgpt"]`, `["INVALID"]`, `[] []`} {
		if err := os.WriteFile(filepath.Join(dir, "catalog.json"), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := loadServiciosCatalog(dir); err == nil {
			t.Fatal("bad catalog accepted", data)
		}
	}
	if filepath.Join(filepath.Dir(manager.RutaSocket("")), "services") != dir {
		t.Fatal("registry path mismatch")
	}
}

func TestServiciosOperatorIntentGate(t *testing.T) {
	a, _, r := serviciosFixture(t)
	var calls []string
	cfg := nativeCooperationConfig(NewApp(WithOperatorGate(func(kind, target string, epoch uint64) bool {
		if epoch != 0 {
			t.Fatal("service authority used lease epoch")
		}
		calls = append(calls, kind+":"+target)
		return true
	})))
	cooperationFor(a).config.Operator = cfg.Operator
	yes, err := r.RequestAccess("ai-a", "srf-ng-1", "chatgpt", "research")
	if err != nil {
		t.Fatal(err)
	}
	no, err := r.RequestAccess("ai-a", "srf-ng-2", "perplexity", "research")
	if err != nil {
		t.Fatal(err)
	}
	for _, op := range []func() error{func() error { return a.ServiciosAsignar("srf-ng-1", "chatgpt") }, func() error { return a.ServiciosDesasignar("srf-ng-1", "chatgpt") }, func() error { return a.ServiciosAprobar(yes.ID) }, func() error { return a.ServiciosDenegar(no.ID) }} {
		if err := op(); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := a.ServiciosEstado(); err != nil {
		t.Fatal(err)
	}
	want := []string{"services-assign:srf-ng-1/chatgpt", "services-unassign:srf-ng-1/chatgpt", "services-approve:" + yes.ID, "services-deny:" + no.ID}
	if !reflect.DeepEqual(calls, want) {
		t.Fatalf("gate calls: %v", calls)
	}
	records, err := wal.LeerRegistros(filepath.Join(os.Getenv("XDG_RUNTIME_DIR"), "os-cu", "cooperation.wal"))
	if err != nil {
		t.Fatal(err)
	}
	var intents []string
	for _, entry := range records {
		if entry.Clave == "operator" {
			var d struct{ Kind, Target string }
			if err := json.Unmarshal(entry.Detalles, &d); err != nil {
				t.Fatal(err)
			}
			intents = append(intents, d.Kind+":"+d.Target)
		}
	}
	if !reflect.DeepEqual(intents, want) {
		t.Fatalf("intent journal: %v", intents)
	}
}
func TestServiciosCatalogSymlinkAndMissingGate(t *testing.T) {
	a, dir, _ := serviciosFixture(t)
	path := filepath.Join(dir, "catalog.json")
	if err := os.Rename(path, path+".old"); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(path+".old", path); err != nil {
		t.Fatal(err)
	}
	if _, err := a.ServiciosEstado(); err == nil {
		t.Fatal("symlink catalog accepted")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(path+".old", path); err != nil {
		t.Fatal(err)
	}
	if err := NewApp().ServiciosAsignar("srf-ng-1", "chatgpt"); err == nil {
		t.Fatal("missing operator gate accepted")
	}
}

func TestServiciosIntentJournalFailureStopsWrite(t *testing.T) {
	a, dir, r := serviciosFixture(t)
	if _, err := r.RequestAccess("ai-a", "srf-ng-1", "chatgpt", "research"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "assignments.json")
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := cooperationFor(a).book.Cerrar(); err != nil {
		t.Fatal(err)
	}
	if err := a.ServiciosAsignar("srf-ng-1", "chatgpt"); err == nil {
		t.Fatal("write accepted after operator journal failure")
	}
	after, err := os.ReadFile(path)
	if err != nil || string(after) != string(before) {
		t.Fatal("journal failure mutated assignments", err)
	}
}
