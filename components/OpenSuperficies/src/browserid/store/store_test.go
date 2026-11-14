package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func bidIdentity(profile string) Identity {
	return Identity{
		ProfileID: profile, Browser: "chromium", AccountLabel: "main account",
		AllowedDomains: []string{"example.co.uk", "google.com"},
	}
}

func bidStore(t *testing.T) *Store {
	t.Helper()
	s, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	s.now = func() time.Time { return time.Unix(1_800_000_000, 0) }
	return s
}

func TestBidStoreCreateGetPrivateModesAudit(t *testing.T) {
	s := bidStore(t)
	want := bidIdentity("google-main")
	if err := s.Create(want); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get(want.ProfileID)
	if err != nil {
		t.Fatal(err)
	}
	if got.ProfileID != want.ProfileID || got.Browser != want.Browser || got.AccountLabel != want.AccountLabel || got.CreatedUnix != 1_800_000_000 || got.LastLoginUnix != nil || len(got.AllowedDomains) != 2 {
		t.Fatalf("record mismatch: %#v", got)
	}
	for _, dir := range []string{s.root, s.metadataDir(), s.profileRoot(), s.lockDir(), filepath.Join(s.profileRoot(), want.ProfileID)} {
		fi, err := os.Stat(dir)
		if err != nil || !fi.IsDir() || fi.Mode().Perm() != 0o700 {
			t.Fatalf("private dir %q: %v / %v", dir, fi, err)
		}
	}
	fi, err := os.Stat(s.metadataPath(want.ProfileID))
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("metadata mode: %v / %v", fi, err)
	}
	audit, err := os.ReadFile(filepath.Join(s.root, "audit.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	var entry map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(audit), &entry); err != nil {
		t.Fatal(err)
	}
	if entry["action"] != "create" || entry["profile_id"] != want.ProfileID || entry["holder"] != "" || entry["result"] != "ok" {
		t.Fatalf("bad audit entry: %v", entry)
	}
}

func TestBidStoreMustFailInvalidIDsTraversal(t *testing.T) {
	s := bidStore(t)
	for _, id := range []string{"", "x", ".", "..", "../profile", "a/b", `a\b`, "bad.id", "-start", "Aupper", "white space", strings.Repeat("z", 49)} {
		t.Run("reject-"+strings.ReplaceAll(id, "/", "_"), func(t *testing.T) {
			if err := s.Create(bidIdentity(id)); !errors.Is(err, ErrInvalid) {
				t.Fatalf("Create(%q) = %v", id, err)
			}
			if _, err := s.Get(id); !errors.Is(err, ErrInvalid) {
				t.Fatalf("Get(%q) = %v", id, err)
			}
			if _, err := s.Acquire(id, "capsule-1"); !errors.Is(err, ErrInvalid) {
				t.Fatalf("Acquire(%q) = %v", id, err)
			}
		})
	}
	if _, err := os.Stat(filepath.Join(s.root, "profile")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("traversal created directory: %v", err)
	}
}

func TestBidStoreMustFailBadMetadataFields(t *testing.T) {
	s := bidStore(t)
	base := bidIdentity("google-main")
	cases := []struct {
		name   string
		change func(*Identity)
	}{
		{"browser", func(i *Identity) { i.Browser = "webkit" }},
		{"label-empty", func(i *Identity) { i.AccountLabel = "" }},
		{"label-control", func(i *Identity) { i.AccountLabel = "abc\nsecret" }},
		{"no-domains", func(i *Identity) { i.AllowedDomains = nil }},
		{"too-many", func(i *Identity) { i.AllowedDomains = make([]string, 17) }},
		{"unsorted", func(i *Identity) { i.AllowedDomains = []string{"google.com", "example.com"} }},
		{"duplicate", func(i *Identity) { i.AllowedDomains = []string{"google.com", "google.com"} }},
		{"upper-domain", func(i *Identity) { i.AllowedDomains = []string{"Google.com"} }},
		{"subdomain", func(i *Identity) { i.AllowedDomains = []string{"login.google.com"} }},
		{"public-suffix", func(i *Identity) { i.AllowedDomains = []string{"co.uk"} }},
		{"not-domain", func(i *Identity) { i.AllowedDomains = []string{"localhost"} }},
		{"trailing-dot", func(i *Identity) { i.AllowedDomains = []string{"google.com."} }},
		{"wildcard", func(i *Identity) { i.AllowedDomains = []string{"*.example.com"} }},
		{"negative-time", func(i *Identity) { i.CreatedUnix = -1 }},
		{"early-login", func(i *Identity) {
			start, login := int64(1234), int64(1233)
			i.CreatedUnix, i.LastLoginUnix = start, &login
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			id := base
			tc.change(&id)
			if err := s.Create(id); !errors.Is(err, ErrInvalid) {
				t.Fatalf("want ErrInvalid, got %v", err)
			}
		})
	}
}

func TestBidStoreMustFailDuplicateCreate(t *testing.T) {
	s := bidStore(t)
	id := bidIdentity("google-main")
	if err := s.Create(id); err != nil {
		t.Fatal(err)
	}
	if err := s.Create(id); !errors.Is(err, ErrExists) {
		t.Fatalf("duplicate = %v", err)
	}
	if _, err := s.Get(id.ProfileID); err != nil {
		t.Fatal(err)
	}
}

func TestBidStoreMustFailConcurrentAcquire(t *testing.T) {
	s := bidStore(t)
	if err := s.Create(bidIdentity("google-main")); err != nil {
		t.Fatal(err)
	}
	const contenders = 12
	start := make(chan struct{})
	type outcome struct {
		lease Lease
		err   error
	}
	results := make(chan outcome, contenders)
	var wg sync.WaitGroup
	for i := 0; i < contenders; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			lease, err := s.Acquire("google-main", "capsule-1")
			results <- outcome{lease, err}
		}()
	}
	close(start)
	wg.Wait()
	close(results)
	success, busy := 0, 0
	var held Lease
	for r := range results {
		if r.err == nil {
			success++
			held = r.lease
			continue
		}
		var errBusy *BusyError
		if !errors.As(r.err, &errBusy) || errBusy.HolderID != "capsule-1" || strings.Contains(errBusy.Error(), s.root) {
			t.Fatalf("not a path-free typed busy error: %v", r.err)
		}
		busy++
	}
	if success != 1 || busy != contenders-1 {
		t.Fatalf("success=%d busy=%d", success, busy)
	}
	if err := s.Release(held); err != nil {
		t.Fatal(err)
	}
}

func TestBidStoreMustFailWrongTokenAndOldLease(t *testing.T) {
	s := bidStore(t)
	if err := s.Create(bidIdentity("chatgpt-1")); err != nil {
		t.Fatal(err)
	}
	lease, err := s.Acquire("chatgpt-1", "capsule-1")
	if err != nil {
		t.Fatal(err)
	}
	wrong := lease
	wrong.Token = "different"
	if err := s.Release(wrong); !errors.Is(err, ErrLeaseMismatch) {
		t.Fatalf("wrong token: %v", err)
	}
	wrong = lease
	wrong.Holder = "capsule-2"
	if err := s.Release(wrong); !errors.Is(err, ErrLeaseMismatch) {
		t.Fatalf("wrong holder: %v", err)
	}
	if _, err := s.Acquire("chatgpt-1", "capsule-2"); err == nil {
		t.Fatal("invalid release unlocked holder")
	}
	if err := s.Release(lease); err != nil {
		t.Fatal(err)
	}
	next, err := s.Acquire("chatgpt-1", "capsule-2")
	if err != nil {
		t.Fatal(err)
	}
	if next.Counter != lease.Counter+1 {
		t.Fatalf("counter did not advance: %d -> %d", lease.Counter, next.Counter)
	}
	if err := s.Release(lease); !errors.Is(err, ErrLeaseMismatch) {
		t.Fatalf("old token released new holder: %v", err)
	}
	if err := s.Release(next); err != nil {
		t.Fatal(err)
	}
}

func TestBidStoreDurableLockExplicitReclaim(t *testing.T) {
	s := bidStore(t)
	if err := s.Create(bidIdentity("perplexity-1")); err != nil {
		t.Fatal(err)
	}
	lease, err := s.Acquire("perplexity-1", "capsule-1")
	if err != nil {
		t.Fatal(err)
	}
	other, err := New(s.root) // simulate a new process without a live in-memory map
	if err != nil {
		t.Fatal(err)
	}
	other.now = s.now
	if _, err := other.Acquire("perplexity-1", "capsule-2"); err == nil {
		t.Fatal("acquired across restart while held")
	}
	if err := other.Reclaim("perplexity-1", lease.Counter+1); !errors.Is(err, ErrLeaseMismatch) {
		t.Fatalf("wrong reclaim counter: %v", err)
	}
	if _, err := other.Acquire("perplexity-1", "capsule-2"); err == nil {
		t.Fatal("wrong reclaim cleared lock")
	}
	if err := other.Reclaim("perplexity-1", lease.Counter); err != nil {
		t.Fatal(err)
	}
	next, err := other.Acquire("perplexity-1", "capsule-2")
	if err != nil {
		t.Fatal(err)
	}
	if next.Counter != lease.Counter+1 {
		t.Fatalf("durable counter mismatch %d -> %d", lease.Counter, next.Counter)
	}
	if err := other.Release(lease); !errors.Is(err, ErrLeaseMismatch) {
		t.Fatalf("stale lease released reclaimed holder: %v", err)
	}
	if err := other.Release(next); err != nil {
		t.Fatal(err)
	}
}

func TestBidStoreMustFailCorruptMetadata(t *testing.T) {
	s := bidStore(t)
	if err := s.Create(bidIdentity("google-main")); err != nil {
		t.Fatal(err)
	}
	path := s.metadataPath("google-main")
	for _, bad := range []string{"{not json", `{"profile_id":"wrong","browser":"chromium","account_label":"a","allowed_domains":["google.com"],"created_unix":1,"last_login_unix":null}`, `{"profile_id":"google-main","browser":"chromium","account_label":"a","allowed_domains":["google.com"],"created_unix":1,"last_login_unix":null,"secret":"no"}`} {
		if err := os.WriteFile(path, []byte(bad), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := s.Get("google-main"); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("corrupt Get = %v", err)
		}
		if _, err := s.Acquire("google-main", "capsule-1"); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("corrupt Acquire = %v", err)
		}
	}
}

func TestBidStoreRecordLoginAndAuditNoToken(t *testing.T) {
	s := bidStore(t)
	if err := s.Create(bidIdentity("google-main")); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordLogin("google-main"); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get("google-main")
	if err != nil || got.LastLoginUnix == nil || *got.LastLoginUnix != 1_800_000_000 {
		t.Fatalf("login: %v / %v", got, err)
	}
	lease, err := s.Acquire("google-main", "capsule-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Release(lease); err != nil {
		t.Fatal(err)
	}
	log, err := os.ReadFile(filepath.Join(s.root, "audit.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(log, []byte(lease.Token)) || bytes.Contains(log, []byte(s.root)) {
		t.Fatal("audit leaked token or path")
	}
	lines := bytes.Split(bytes.TrimSpace(log), []byte("\n"))
	if len(lines) != 4 {
		t.Fatalf("audit lines = %d, expected create/login/acquire/release", len(lines))
	}
	for i, action := range []string{"create", "login", "acquire", "release"} {
		var m map[string]any
		if err := json.Unmarshal(lines[i], &m); err != nil {
			t.Fatal(err)
		}
		if m["action"] != action || m["result"] != "ok" {
			t.Fatalf("audit[%d] = %v", i, m)
		}
	}
}

// The fake filesystem makes any attempt to OPEN a path inside the opaque
// browser profile fail. mkdir/lstat on the profile directory itself is allowed.
type denyProfileOpen struct {
	diskFiles
	profileRoot string
	attempts    int
}

func (p *denyProfileOpen) OpenFile(path string, flags int, mode os.FileMode) (*os.File, error) {
	rel, err := filepath.Rel(p.profileRoot, path)
	if err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		p.attempts++
		return nil, errors.New("attempted to open opaque profile payload")
	}
	return p.diskFiles.OpenFile(path, flags, mode)
}

func TestBidStoreMustFailProfilePayloadRead(t *testing.T) {
	root := t.TempDir()
	port := &denyProfileOpen{profileRoot: filepath.Join(root, "profiles")}
	s, err := newWithPort(root, port, func() time.Time { return time.Unix(1_800_000_000, 0) })
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Create(bidIdentity("google-main")); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Get("google-main"); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordLogin("google-main"); err != nil {
		t.Fatal(err)
	}
	lease, err := s.Acquire("google-main", "capsule-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Release(lease); err != nil {
		t.Fatal(err)
	}
	next, err := s.Acquire("google-main", "capsule-2")
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Reclaim("google-main", next.Counter); err != nil {
		t.Fatal(err)
	}
	if port.attempts != 0 {
		t.Fatalf("attempted %d profile payload opens", port.attempts)
	}
}

func TestBidStoreMustFailOrphanProfileAndMetadataSymlink(t *testing.T) {
	s := bidStore(t)
	orphan := filepath.Join(s.profileRoot(), "google-main")
	if err := os.Mkdir(orphan, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := s.Create(bidIdentity("google-main")); !errors.Is(err, ErrExists) {
		t.Fatalf("orphan profile adopted: %v", err)
	}
	// A symlink in metadata must not allow a read through to profile payloads.
	path := s.metadataPath("google-main")
	if err := os.Symlink(filepath.Join(orphan, "opaque"), path); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Get("google-main"); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("metadata symlink followed: %v", err)
	}
}

func TestBidStoreBoundaries(t *testing.T) {
	s := bidStore(t)
	id := bidIdentity("ab") // min length
	id.Browser = "firefox"
	id.AllowedDomains = []string{"example.com"}
	if err := s.Create(id); err != nil {
		t.Fatal(err)
	}
	id.ProfileID = strings.Repeat("z", 48) // max length
	id.AllowedDomains = []string{}
	for i := 0; i < 16; i++ {
		id.AllowedDomains = append(id.AllowedDomains, "a"+strings.Repeat("b", i+1)+".com")
	}
	if err := s.Create(id); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Get(id.ProfileID); err != nil {
		t.Fatal(err)
	}
}

func TestProgramIdentityRetainsDomainRules(t *testing.T) {
	s := bidStore(t)
	id := bidIdentity("notepad-main")
	id.Browser = "program"
	if e := s.Create(id); e != nil {
		t.Fatal(e)
	}
	got, e := s.Get(id.ProfileID)
	if e != nil || got.Browser != "program" {
		t.Fatal("program record", e)
	}
	id.ProfileID = "bad-program"
	id.AllowedDomains = []string{"../unsafe"}
	if e := s.Create(id); !errors.Is(e, ErrInvalid) {
		t.Fatal("domain rules bypassed", e)
	}
}
