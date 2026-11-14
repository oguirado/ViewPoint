package services

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"syscall"
	"testing"
)

type catalog map[string]bool

func (c catalog) HasService(s string) bool { return c[s] }
func (c catalog) Services() []string {
	var a []string
	for s := range c {
		a = append(a, s)
	}
	return a
}

type credential struct{ err error }

func (c credential) AuthorizeServices() error { return c.err }
func setup(t *testing.T) (*Registry, OperatorToken, string) {
	t.Helper()
	d := t.TempDir()
	r, tok, e := OpenOperator(d, catalog{"chatgpt": true, "perplexity": true}, credential{})
	if e != nil {
		t.Fatal(e)
	}
	return r, tok, d
}
func TestRoundTripAndToken(t *testing.T) {
	r, tok, d := setup(t)
	if e := r.Assign(nil, "capsule-1", "chatgpt"); !errors.Is(e, ErrUnauthorized) {
		t.Fatal(e)
	}
	other, foreign, _ := OpenOperator(t.TempDir(), catalog{"chatgpt": true}, credential{})
	_ = other
	if e := r.Assign(foreign, "capsule-1", "chatgpt"); !errors.Is(e, ErrUnauthorized) {
		t.Fatal(e)
	}
	if _, _, e := OpenOperator(d, catalog{}, credential{errors.New("denied")}); e == nil {
		t.Fatal("credential accepted")
	}
	for _, s := range []string{"perplexity", "chatgpt"} {
		if e := r.Assign(tok, "capsule-1", s); e != nil {
			t.Fatal(e)
		}
	}
	reopened, e := Open(d, catalog{"chatgpt": true, "perplexity": true})
	if e != nil {
		t.Fatal(e)
	}
	status, e := reopened.Status("capsule-1")
	if e != nil || !reflect.DeepEqual(status.Services, []string{"chatgpt", "perplexity"}) || status.Revision != 2 {
		t.Fatalf("%+v %v", status, e)
	}
	if e := r.Unassign(tok, "capsule-1", "chatgpt"); e != nil {
		t.Fatal(e)
	}
	st, _ := r.Status("capsule-1")
	if st.Revision != 3 {
		t.Fatal(st)
	}
	for _, name := range []string{"assignments.json", "journal.jsonl"} {
		info, e := os.Stat(filepath.Join(d, name))
		if e != nil || info.Mode().Perm() != 0600 {
			t.Fatalf("permissions %s %v", name, e)
		}
	}
}
func TestQueries(t *testing.T) {
	r, tok, _ := setup(t)
	for _, id := range []string{"capsule-2", "capsule-1", "srf-ng-worker"} {
		if e := r.Assign(tok, id, "chatgpt"); e != nil {
			t.Fatal(e)
		}
	}
	for _, tc := range []struct {
		s        string
		approved bool
		n        int
		every    bool
	}{{"chatgpt", true, 3, true}, {"perplexity", true, 0, false}, {"unknown", false, 0, false}} {
		q, e := r.WhereCanOpen(tc.s)
		if e != nil || q.ApprovedInCatalog != tc.approved || len(q.AvailableIn) != tc.n || q.Everywhere != tc.every {
			t.Fatalf("%+v %v", q, e)
		}
	}
	if e := r.Unassign(tok, "capsule-1", "chatgpt"); e != nil {
		t.Fatal(e)
	}
	q, _ := r.WhereCanOpen("chatgpt")
	if q.Everywhere {
		t.Fatal("empty known capsule ignored")
	}
	for _, bad := range []string{"capsule-1000", "capsule-x", "srf-ng-", "../capsule-1"} {
		if _, e := r.Status(bad); e == nil {
			t.Fatal(bad)
		}
	}
}
func TestRequests(t *testing.T) {
	r, tok, _ := setup(t)
	a, e := r.RequestAccess("ai-a", "capsule-1", "chatgpt", "research")
	if e != nil {
		t.Fatal(e)
	}
	b, e := r.RequestAccess("ai-a", "capsule-1", "chatgpt", "research")
	if e != nil || a.ID != b.ID {
		t.Fatal("dedup", e)
	}
	if e := r.Approve(nil, a.ID); !errors.Is(e, ErrUnauthorized) {
		t.Fatal(e)
	}
	if e := r.Approve(tok, a.ID); e != nil {
		t.Fatal(e)
	}
	if e := r.Deny(tok, a.ID); !errors.Is(e, ErrNotPending) {
		t.Fatal(e)
	}
	st, _ := r.Status("capsule-1")
	if len(st.Services) != 1 {
		t.Fatal(st)
	}
	b, _ = r.RequestAccess("ai-a", "capsule-2", "chatgpt", "other")
	if e := r.Deny(tok, b.ID); e != nil {
		t.Fatal(e)
	}
	for i := range 20 {
		if _, e := r.RequestAccess("ai-cap", "capsule-1", "chatgpt", fmt.Sprint(i)); e != nil {
			t.Fatal(e)
		}
	}
	if _, e := r.RequestAccess("ai-cap", "capsule-1", "chatgpt", "overflow"); !errors.Is(e, ErrPendingLimit) {
		t.Fatal(e)
	}
	if _, e := r.RequestAccess("ai-a", "capsule-1", "unknown", "why"); !errors.Is(e, ErrUnknownService) {
		t.Fatal(e)
	}
	for _, reason := range []string{"bad\nreason", string(make([]byte, 281))} {
		if _, e := r.RequestAccess("ai-a", "capsule-1", "chatgpt", reason); e == nil {
			t.Fatal("reason accepted")
		}
	}
	p, e := r.ListPending()
	if e != nil || len(p) != 20 {
		t.Fatal(len(p), e)
	}
}
func TestCorruption(t *testing.T) {
	_, _, d := setup(t)
	for _, bad := range []string{`{`, `{"schema":"wrong"}`, `{"schema":"browserid.assignments/v1","revision":0,"assignments":{},"requests":[],"journal":[],"extra":true}`} {
		if e := os.WriteFile(filepath.Join(d, "assignments.json"), []byte(bad), 0600); e != nil {
			t.Fatal(e)
		}
		if _, e := Open(d, catalog{}); e == nil {
			t.Fatal("corrupt accepted", bad)
		}
	}
}
func TestConcurrency(t *testing.T) {
	r, tok, d := setup(t)
	reader, e := Open(d, catalog{"chatgpt": true})
	if e != nil {
		t.Fatal(e)
	}
	var wg sync.WaitGroup
	for i := range 30 {
		wg.Go(func() {
			id := fmt.Sprintf("capsule-%d", i)
			if e := r.Assign(tok, id, "chatgpt"); e != nil {
				t.Error(e)
			}
			if _, e := reader.WhereCanOpen("chatgpt"); e != nil {
				t.Error(e)
			}
			if _, e := reader.RequestAccess("parallel", id, "chatgpt", id); e != nil && !errors.Is(e, ErrPendingLimit) {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	s, _ := r.Status("capsule-0")
	if s.Revision < 30 {
		t.Fatal(s)
	}
}

func TestJournalAndCatalogRevocation(t *testing.T) {
	r, tok, d := setup(t)
	if e := r.Assign(tok, "capsule-1", "chatgpt"); e != nil {
		t.Fatal(e)
	}
	if e := r.Assign(tok, "capsule-1", "chatgpt"); e != nil {
		t.Fatal(e)
	}
	req, e := r.RequestAccess("ai-a", "capsule-2", "chatgpt", "need it")
	if e != nil {
		t.Fatal(e)
	}
	before, _ := r.Status("capsule-1")
	if _, e = r.RequestAccess("ai-a", "capsule-2", "chatgpt", "need it"); e != nil {
		t.Fatal(e)
	}
	after, _ := r.Status("capsule-1")
	if before.Revision != after.Revision {
		t.Fatal("dedup advanced revision")
	}
	if e = r.Deny(tok, req.ID); e != nil {
		t.Fatal(e)
	}
	data, e := os.ReadFile(filepath.Join(d, "journal.jsonl"))
	if e != nil {
		t.Fatal(e)
	}
	if bytes.Count(data, []byte{'\n'}) != 3 {
		t.Fatalf("journal: %s", data)
	}
	var last JournalEntry
	lines := bytes.Split(bytes.TrimSpace(data), []byte{'\n'})
	if e = json.Unmarshal(lines[2], &last); e != nil || last.Action != "deny" || last.Actor != "operator" || last.Revision != 4 {
		t.Fatalf("%+v %v", last, e)
	}
	revoked, e := Open(d, catalog{})
	if e != nil {
		t.Fatal(e)
	}
	q, e := revoked.WhereCanOpen("chatgpt")
	if e != nil || q.ApprovedInCatalog || len(q.AvailableIn) != 0 {
		t.Fatal(q, e)
	}
	st, _ := revoked.Status("capsule-1")
	if len(st.Services) != 0 {
		t.Fatal(st)
	}
	// An atomically replaced journal may be a previous complete prefix after a crash.
	if e = os.WriteFile(filepath.Join(d, "journal.jsonl"), append(lines[0], '\n'), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e = Open(d, catalog{}); e != nil {
		t.Fatal(e)
	}
	recovered, _ := os.ReadFile(filepath.Join(d, "journal.jsonl"))
	if !bytes.Equal(recovered, data) {
		t.Fatal("journal not recovered")
	}
	if e = os.WriteFile(filepath.Join(d, "journal.jsonl"), []byte("tampered\n"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e = Open(d, catalog{}); !errors.Is(e, ErrCorrupt) {
		t.Fatal("journal corruption accepted", e)
	}
}
func TestIndependentWriters(t *testing.T) {
	r, a, d := setup(t)
	other, b, e := OpenOperator(d, catalog{"chatgpt": true}, credential{})
	if e != nil {
		t.Fatal(e)
	}
	var wg sync.WaitGroup
	for i := range 40 {
		wg.Go(func() {
			writer, token := r, a
			if i%2 == 0 {
				writer, token = other, b
			}
			if e := writer.Assign(token, fmt.Sprintf("capsule-%d", i), "chatgpt"); e != nil {
				t.Error(e)
			}
		})
	}
	wg.Wait()
	q, e := r.WhereCanOpen("chatgpt")
	if e != nil || len(q.AvailableIn) != 40 {
		t.Fatal(q, e)
	}
	st, _ := r.Status("capsule-1")
	if st.Revision != 40 {
		t.Fatal(st)
	}
}
func TestUnsafeFiles(t *testing.T) {
	var nilCredential *credential
	if _, _, e := OpenOperator(t.TempDir(), catalog{}, nilCredential); !errors.Is(e, ErrUnauthorized) {
		t.Fatal(e)
	}
	d := t.TempDir()
	outside := filepath.Join(t.TempDir(), "state")
	if e := os.WriteFile(outside, []byte("do not touch"), 0600); e != nil {
		t.Fatal(e)
	}
	if e := os.Symlink(outside, filepath.Join(d, "assignments.json")); e != nil {
		t.Fatal(e)
	}
	if _, e := Open(d, catalog{}); e == nil {
		t.Fatal("followed state symlink")
	}
	b, _ := os.ReadFile(outside)
	if string(b) != "do not touch" {
		t.Fatal("outside state changed")
	}
}

func TestNonRegularStateRefused(t *testing.T) {
	d := t.TempDir()
	if e := syscall.Mkfifo(filepath.Join(d, "assignments.json"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := Open(d, catalog{}); !errors.Is(e, ErrCorrupt) {
		t.Fatal(e)
	}
}

func TestUninitializedRegistryRefused(t *testing.T) {
	for _, r := range []*Registry{nil, {}} {
		if _, e := r.Status("capsule-1"); !errors.Is(e, ErrInvalid) {
			t.Fatal(e)
		}
		if e := r.Assign(nil, "capsule-1", "chatgpt"); !errors.Is(e, ErrUnauthorized) {
			t.Fatal(e)
		}
	}
}
