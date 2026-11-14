// Package services stores operator assignments and AI access requests. It does
// not own domain policy, authenticate operators, or confer browser leases.
package services

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"
	"unicode/utf8"
)

const Schema = "browserid.assignments/v1"
const maxStateBytes = 16 * 1024 * 1024

var (
	ErrUnauthorized   = errors.New("operator capability required")
	ErrUnknownService = errors.New("service is not approved in catalog")
	ErrInvalid        = errors.New("invalid input")
	ErrCorrupt        = errors.New("corrupt registry")
	ErrPendingLimit   = errors.New("pending request limit reached")
	ErrNotPending     = errors.New("request is not pending")
)

// Catalog is owned by the egress package. Implementations must be safe for
// concurrent reads and must return a consistent approved service snapshot.
type Catalog interface {
	HasService(string) bool
	Services() []string
}

// OperatorCredential is injected only by a trusted operator host. Its method
// must authenticate the operator, returning an error on refusal. It is never a
// string or a credential loaded by this package or its CLI.
type OperatorCredential interface{ AuthorizeServices() error }

// OperatorToken is sealed: callers cannot implement or construct a valid token.
type OperatorToken interface{ servicesOperator() }
type operatorToken struct{ registry *Registry }

func (*operatorToken) servicesOperator() {}

type Registry struct {
	mu      sync.Mutex
	dir     string
	catalog Catalog
	token   *operatorToken
}
type Request struct {
	ID       string `json:"id"`
	AIID     string `json:"ai_id"`
	Capsule  string `json:"capsule"`
	Service  string `json:"service"`
	Reason   string `json:"reason"`
	State    string `json:"state"`
	Revision uint64 `json:"revision"`
}
type JournalEntry struct {
	Time     string `json:"time"`
	Actor    string `json:"actor"`
	Action   string `json:"action"`
	Capsule  string `json:"capsule"`
	Service  string `json:"service"`
	Revision uint64 `json:"revision"`
}
type state struct {
	Schema      string              `json:"schema"`
	Revision    uint64              `json:"revision"`
	Assignments map[string][]string `json:"assignments"`
	Requests    []Request           `json:"requests"`
	Journal     []JournalEntry      `json:"journal"`
}
type Query struct {
	Service           string   `json:"service"`
	AvailableIn       []string `json:"available_in"`
	Everywhere        bool     `json:"everywhere"`
	ApprovedInCatalog bool     `json:"approved_in_catalog"`
}
type CapsuleStatus struct {
	Capsule  string   `json:"capsule"`
	Services []string `json:"services"`
	Revision uint64   `json:"revision"`
}

var capsulePattern = regexp.MustCompile(`^(capsule-[0-9]{1,3}|srf-ng-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119})$`)
var servicePattern = regexp.MustCompile(`^[a-z][a-z0-9_-]{1,31}$`)
var aiPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`)

func nilLike(v any) bool {
	if v == nil {
		return true
	}
	x := reflect.ValueOf(v)
	switch x.Kind() {
	case reflect.Pointer, reflect.Map, reflect.Slice, reflect.Func, reflect.Interface:
		return x.IsNil()
	}
	return false
}
func Open(dir string, catalog Catalog) (*Registry, error) {
	if dir == "" || nilLike(catalog) {
		return nil, ErrInvalid
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, ErrInvalid
	}
	if err := os.Chmod(dir, 0700); err != nil {
		return nil, err
	}
	r := &Registry{dir: dir, catalog: catalog}
	err = r.withState(false, func(*state) error { return nil })
	if err != nil {
		return nil, err
	}
	return r, nil
}
func OpenOperator(dir string, catalog Catalog, credential OperatorCredential) (*Registry, OperatorToken, error) {
	if nilLike(credential) {
		return nil, nil, ErrUnauthorized
	}
	if err := credential.AuthorizeServices(); err != nil {
		return nil, nil, ErrUnauthorized
	}
	r, err := Open(dir, catalog)
	if err != nil {
		return nil, nil, err
	}
	r.token = &operatorToken{r}
	return r, r.token, nil
}
func (r *Registry) authorized(token OperatorToken) bool {
	if r == nil {
		return false
	}
	t, ok := token.(*operatorToken)
	return ok && t != nil && r.token != nil && t == r.token && t.registry == r
}
func validatePair(capsule, service string) error {
	if !capsulePattern.MatchString(capsule) || !servicePattern.MatchString(service) {
		return ErrInvalid
	}
	return nil
}
func validReason(s string) bool {
	if !utf8.ValidString(s) || utf8.RuneCountInString(s) > 280 {
		return false
	}
	for _, c := range s {
		if !unicode.IsPrint(c) {
			return false
		}
	}
	return true
}
func emptyState() *state {
	return &state{Schema: Schema, Assignments: map[string][]string{}, Requests: []Request{}, Journal: []JournalEntry{}}
}

// secureOpen refuses symlinks and non-regular files; caller directory must be
// private and trusted. Flock serializes independent handles and processes.
func secureOpen(path string, flags int) (*os.File, error) {
	fd, e := syscall.Open(path, flags|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0600)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), path)
	s, e := f.Stat()
	if e != nil || !s.Mode().IsRegular() || s.Mode().Perm() != 0600 {
		f.Close()
		return nil, ErrCorrupt
	}
	return f, nil
}
func (r *Registry) withState(write bool, fn func(*state) error) error {
	if r == nil || r.dir == "" || nilLike(r.catalog) {
		return ErrInvalid
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	lock, e := secureOpen(filepath.Join(r.dir, ".lock"), syscall.O_CREAT|syscall.O_RDWR)
	if e != nil {
		return e
	}
	defer lock.Close()
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); e != nil {
		return e
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	s, e := r.load()
	if e != nil {
		return e
	}
	if e = r.projectJournal(s); e != nil {
		return e
	}
	if e = fn(s); e != nil {
		return e
	}
	if !write {
		return nil
	}
	if s.Revision == ^uint64(0) {
		return ErrInvalid
	}
	s.Revision++
	data, e := json.Marshal(s)
	if e != nil {
		return e
	}
	data = append(data, '\n')
	if len(data) > maxStateBytes {
		return fmt.Errorf("%w: state capacity exceeded", ErrInvalid)
	}
	if e = r.atomic("assignments.json", data); e != nil {
		return e
	}
	return r.projectJournal(s)
}
func (r *Registry) load() (*state, error) {
	f, e := secureOpen(filepath.Join(r.dir, "assignments.json"), syscall.O_RDONLY)
	if errors.Is(e, os.ErrNotExist) { // A journal without state means lost authority, not a fresh registry.
		if _, e := os.Lstat(filepath.Join(r.dir, "journal.jsonl")); !errors.Is(e, os.ErrNotExist) {
			return nil, ErrCorrupt
		}
		return emptyState(), nil
	}
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, maxStateBytes+1))
	if e != nil {
		return nil, e
	}
	if len(b) > maxStateBytes {
		return nil, ErrCorrupt
	}
	var s state
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e = d.Decode(&s); e != nil {
		return nil, fmt.Errorf("%w: %v", ErrCorrupt, e)
	}
	if d.Decode(new(any)) != io.EOF {
		return nil, ErrCorrupt
	}
	canonical, _ := json.Marshal(s)
	canonical = append(canonical, '\n')
	if !bytes.Equal(b, canonical) {
		return nil, ErrCorrupt
	}
	if s.Schema != Schema || s.Assignments == nil || s.Requests == nil || s.Journal == nil {
		return nil, ErrCorrupt
	}
	for id, services := range s.Assignments {
		if !capsulePattern.MatchString(id) || services == nil || !slices.IsSorted(services) {
			return nil, ErrCorrupt
		}
		for i, name := range services {
			if !servicePattern.MatchString(name) || (i > 0 && services[i-1] == name) {
				return nil, ErrCorrupt
			}
		}
	}
	ids := map[string]bool{}
	pending := map[string]int{}
	keys := map[string]bool{}
	for _, q := range s.Requests {
		if validatePair(q.Capsule, q.Service) != nil || !aiPattern.MatchString(q.AIID) || !validReason(q.Reason) || q.Revision == 0 || q.Revision > s.Revision || q.ID != requestID(q.AIID, q.Capsule, q.Service, q.Reason, q.Revision) || ids[q.ID] {
			return nil, ErrCorrupt
		}
		ids[q.ID] = true
		switch q.State {
		case "pending":
			pending[q.AIID]++
			k := requestID(q.AIID, q.Capsule, q.Service, q.Reason, 0)
			if keys[k] || pending[q.AIID] > 20 {
				return nil, ErrCorrupt
			}
			keys[k] = true
		case "approved", "denied":
		default:
			return nil, ErrCorrupt
		}
	}
	var rev uint64
	for _, j := range s.Journal {
		if j.Actor != "operator" || validatePair(j.Capsule, j.Service) != nil || j.Revision <= rev || j.Revision > s.Revision {
			return nil, ErrCorrupt
		}
		if _, e := time.Parse(time.RFC3339Nano, j.Time); e != nil {
			return nil, ErrCorrupt
		}
		switch j.Action {
		case "assign", "unassign", "approve", "deny":
		default:
			return nil, ErrCorrupt
		}
		rev = j.Revision
	}
	return &s, nil
}
func (r *Registry) atomic(name string, data []byte) error {
	f, e := os.CreateTemp(r.dir, ".services-*")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if e = f.Chmod(0600); e != nil {
		return e
	}
	if _, e = f.Write(data); e != nil {
		return e
	}
	if e = f.Sync(); e != nil {
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	target := filepath.Join(r.dir, name)
	if info, e := os.Lstat(target); e == nil && (!info.Mode().IsRegular() || info.Mode().Perm() != 0600) {
		return ErrCorrupt
	} else if e != nil && !errors.Is(e, os.ErrNotExist) {
		return e
	}
	if e = os.Rename(f.Name(), target); e != nil {
		return e
	}
	dir, e := os.Open(r.dir)
	if e != nil {
		return e
	}
	defer dir.Close()
	return dir.Sync()
}

// Journal is a recoverable JSONL projection of the authoritative atomic state.
// This makes a crash between the state rename and journal refresh recoverable.
func (r *Registry) projectJournal(s *state) error {
	var b bytes.Buffer
	for _, j := range s.Journal {
		line, _ := json.Marshal(j)
		b.Write(line)
		b.WriteByte('\n')
	}
	path := filepath.Join(r.dir, "journal.jsonl")
	f, e := secureOpen(path, syscall.O_RDONLY)
	if errors.Is(e, os.ErrNotExist) {
		if s.Revision == 0 {
			return nil
		}
		return r.atomic("journal.jsonl", b.Bytes())
	}
	if e != nil {
		return e
	}
	old, e := io.ReadAll(io.LimitReader(f, int64(b.Len())+1))
	f.Close()
	if e != nil {
		return e
	}
	if bytes.Equal(old, b.Bytes()) {
		return nil
	}
	// Only a complete earlier prefix is a recoverable crash window. Arbitrary
	// edits or appended lines fail closed rather than erasing evidence.
	if !bytes.HasPrefix(b.Bytes(), old) || (len(old) > 0 && old[len(old)-1] != '\n') {
		return ErrCorrupt
	}
	return r.atomic("journal.jsonl", b.Bytes())
}
func addAssignment(s *state, capsule, service string) {
	a := s.Assignments[capsule]
	if !slices.Contains(a, service) {
		a = append(a, service)
		slices.Sort(a)
	}
	s.Assignments[capsule] = a
}
func journal(s *state, action, capsule, service string) {
	s.Journal = append(s.Journal, JournalEntry{time.Now().UTC().Format(time.RFC3339Nano), "operator", action, capsule, service, s.Revision + 1})
}
func (r *Registry) Assign(token OperatorToken, capsule, service string) error {
	if !r.authorized(token) {
		return ErrUnauthorized
	}
	if e := validatePair(capsule, service); e != nil {
		return e
	}
	return r.withState(true, func(s *state) error {
		if !r.catalog.HasService(service) {
			return ErrUnknownService
		}
		addAssignment(s, capsule, service)
		journal(s, "assign", capsule, service)
		return nil
	})
}
func (r *Registry) Unassign(token OperatorToken, capsule, service string) error {
	if !r.authorized(token) {
		return ErrUnauthorized
	}
	if e := validatePair(capsule, service); e != nil {
		return e
	}
	return r.withState(true, func(s *state) error {
		a := s.Assignments[capsule]
		a = slices.DeleteFunc(a, func(v string) bool { return v == service })
		if a == nil {
			a = []string{}
		}
		s.Assignments[capsule] = a
		journal(s, "unassign", capsule, service)
		return nil
	})
}
func (r *Registry) WhereCanOpen(service string) (Query, error) {
	q := Query{Service: service, AvailableIn: []string{}}
	if !servicePattern.MatchString(service) {
		return q, ErrInvalid
	}
	e := r.withState(false, func(s *state) error {
		q.ApprovedInCatalog = r.catalog.HasService(service)
		if q.ApprovedInCatalog {
			for id, a := range s.Assignments {
				if slices.Contains(a, service) {
					q.AvailableIn = append(q.AvailableIn, id)
				}
			}
		}
		slices.Sort(q.AvailableIn)
		q.Everywhere = q.ApprovedInCatalog && len(s.Assignments) > 0 && len(q.AvailableIn) == len(s.Assignments)
		return nil
	})
	return q, e
}
func (r *Registry) Status(capsule string) (CapsuleStatus, error) {
	v := CapsuleStatus{Capsule: capsule, Services: []string{}}
	if !capsulePattern.MatchString(capsule) {
		return v, ErrInvalid
	}
	e := r.withState(false, func(s *state) error {
		v.Revision = s.Revision
		for _, name := range s.Assignments[capsule] {
			if r.catalog.HasService(name) {
				v.Services = append(v.Services, name)
			}
		}
		return nil
	})
	return v, e
}
func requestID(ai, capsule, service, reason string, rev uint64) string {
	b, _ := json.Marshal([]any{ai, capsule, service, reason, rev})
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

var errDedup = errors.New("deduplicated")

func (r *Registry) RequestAccess(ai, capsule, service, reason string) (Request, error) {
	var q Request
	if !aiPattern.MatchString(ai) || validatePair(capsule, service) != nil || !validReason(reason) {
		return q, ErrInvalid
	}
	e := r.withState(true, func(s *state) error {
		if !r.catalog.HasService(service) {
			return ErrUnknownService
		}
		n := 0
		for _, p := range s.Requests {
			if p.State == "pending" && p.AIID == ai {
				n++
				if p.Capsule == capsule && p.Service == service && p.Reason == reason {
					q = p
					return errDedup
				}
			}
		}
		if n >= 20 {
			return ErrPendingLimit
		}
		q = Request{requestID(ai, capsule, service, reason, s.Revision+1), ai, capsule, service, reason, "pending", s.Revision + 1}
		s.Requests = append(s.Requests, q)
		return nil
	})
	if errors.Is(e, errDedup) {
		e = nil
	}
	return q, e
}
func (r *Registry) ListPending() ([]Request, error) {
	a := []Request{}
	e := r.withState(false, func(s *state) error {
		for _, q := range s.Requests {
			if q.State == "pending" {
				a = append(a, q)
			}
		}
		slices.SortFunc(a, func(a, b Request) int { return strings.Compare(a.ID, b.ID) })
		return nil
	})
	return a, e
}
func (r *Registry) Approve(token OperatorToken, id string) error { return r.decide(token, id, true) }
func (r *Registry) Deny(token OperatorToken, id string) error    { return r.decide(token, id, false) }
func (r *Registry) decide(token OperatorToken, id string, approve bool) error {
	if !r.authorized(token) {
		return ErrUnauthorized
	}
	return r.withState(true, func(s *state) error {
		for i, q := range s.Requests {
			if q.ID != id || q.State != "pending" {
				continue
			}
			action := "deny"
			s.Requests[i].State = "denied"
			if approve {
				if !r.catalog.HasService(q.Service) {
					return ErrUnknownService
				}
				action = "approve"
				s.Requests[i].State = "approved"
				addAssignment(s, q.Capsule, q.Service)
			}
			journal(s, action, q.Capsule, q.Service)
			return nil
		}
		return ErrNotPending
	})
}
