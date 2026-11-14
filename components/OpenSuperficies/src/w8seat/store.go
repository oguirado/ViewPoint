package w8seat

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

type Attempt struct {
	BodyReaped bool            `json:"body_reaped"`
	Grant      json.RawMessage `json:"grant"`
	Handle     string          `json:"handle"`
	Digest     string          `json:"digest"`
	UID        uint32          `json:"uid"`
	Task       json.RawMessage `json:"task"`
	Tip        string          `json:"observed_integration_tip"`
	Container  string          `json:"container"`
	Epoch      uint64          `json:"epoch"`
	Created    int64           `json:"created_at_ms"`
	Expires    int64           `json:"expires_at_ms"`
	Acked      bool            `json:"acked"`
	State      string          `json:"state"`
	Response   Response        `json:"response"`
}

// Store persists the attempt identity before a lease, admission or create. A
// restart never resubmits an uncertain attempt; the janitor owns its rc6.
type Store struct {
	mu   sync.Mutex
	dir  string
	rows map[string]*Attempt
	lock *os.File
}

func OpenStore(dir string) (*Store, error) {
	if e := os.MkdirAll(dir, 0700); e != nil {
		return nil, e
	}
	st, e := os.Lstat(dir)
	if e != nil || !st.IsDir() || st.Mode().Perm()&0077 != 0 {
		return nil, refusal(6, "indeterminate", "unsafe seat state directory")
	}
	stat, ok := st.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != os.Geteuid() {
		return nil, refusal(6, "indeterminate", "state owner")
	}
	fd, e := syscall.Open(filepath.Join(dir, "seat.lock"), syscall.O_CREAT|syscall.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), "seat.lock")
	if e = syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		f.Close()
		return nil, refusal(4, "unavailable", "seat already running")
	}
	s := &Store{dir: dir, rows: map[string]*Attempt{}, lock: f}
	b, e := secureRead(filepath.Join(dir, "attempts.json"), 64<<20, false)
	if e != nil {
		if _, e2 := os.Lstat(filepath.Join(dir, "attempts.json")); !os.IsNotExist(e2) {
			s.Close()
			return nil, refusal(6, "indeterminate", "attempt table unreadable")
		}
	} else if strictJSON(b, &s.rows) != nil || s.rows == nil {
		s.Close()
		return nil, refusal(6, "indeterminate", "attempt table corrupt")
	}
	for k, a := range s.rows {
		if !hex64.MatchString(k) || a == nil || a.Handle != k || !hex64.MatchString(a.Digest) || len(a.Task) > MaxHeader {
			s.Close()
			return nil, refusal(6, "indeterminate", "attempt identity corrupt")
		}
	}
	return s, nil
}
func (s *Store) Close() {
	if s.lock != nil {
		_ = syscall.Flock(int(s.lock.Fd()), syscall.LOCK_UN)
		_ = s.lock.Close()
		s.lock = nil
	}
}
func atomicWrite(path string, b []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	f, e := os.CreateTemp(dir, ".w8-stage-")
	if e != nil {
		return e
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if e = f.Chmod(mode); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e != nil {
		return e
	}
	if e = os.Rename(tmp, path); e != nil {
		return e
	}
	d, e := os.Open(dir)
	if e != nil {
		return e
	}
	defer d.Close()
	return d.Sync()
}
func (s *Store) saveLocked() error {
	b, e := json.Marshal(s.rows)
	if e != nil || len(b) > 64<<20 {
		return refusal(4, "unavailable", "body/table quota")
	}
	if e = atomicWrite(filepath.Join(s.dir, "attempts.json"), b, 0600); e != nil {
		return refusal(6, "indeterminate", "attempt table sync failed")
	}
	return nil
}
func cloneAttempt(a *Attempt) *Attempt {
	b, _ := json.Marshal(a)
	var c Attempt
	_ = json.Unmarshal(b, &c)
	return &c
}
func (s *Store) Get(handle string) (*Attempt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	a := s.rows[handle]
	if a == nil {
		return nil, refusal(2, "contract", "unknown attempt")
	}
	return cloneAttempt(a), nil
}
func (s *Store) All() []*Attempt {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*Attempt, 0, len(s.rows))
	for _, a := range s.rows {
		out = append(out, cloneAttempt(a))
	}
	return out
}
func (s *Store) Reserve(a *Attempt) (*Attempt, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if old := s.rows[a.Handle]; old != nil {
		if old.Digest != a.Digest || old.UID != a.UID {
			return nil, false, refusal(2, "contract", "attempt digest/owner differs")
		}
		return cloneAttempt(old), false, nil
	}
	active := 0
	bytes := 0
	for _, x := range s.rows {
		if !x.Acked {
			active++
		}
		bytes += len(x.Task)
		for _, v := range x.Response.Artifacts {
			bytes += 2 * len(v)
		}
		bytes += 2 * len(x.Response.Receipt)
	}
	if active >= 512 || bytes+len(a.Task)+2*MaxText+2*65536 > 64<<20 {
		return nil, false, refusal(4, "unavailable", "body store quota")
	}
	s.rows[a.Handle] = cloneAttempt(a)
	if e := s.saveLocked(); e != nil {
		delete(s.rows, a.Handle)
		return nil, false, e
	}
	return cloneAttempt(a), true, nil
}
func (s *Store) Update(handle string, fn func(*Attempt)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	a := s.rows[handle]
	if a == nil {
		return refusal(2, "contract", "attempt absent")
	}
	old := cloneAttempt(a)
	fn(a)
	if e := s.saveLocked(); e != nil {
		s.rows[handle] = old
		return e
	}
	return nil
}
func (s *Store) Ack(handle string) error {
	return s.Update(handle, func(a *Attempt) { a.Acked = true })
}
func (s *Store) Reap(now time.Time, journal func(string) error) error {
	for _, a := range s.All() {
		if a.BodyReaped || len(a.Response.Receipt) == 0 || (!a.Acked && now.UnixMilli()-a.Created < 24*time.Hour.Milliseconds()) {
			continue
		}
		if e := journal("W8_BODY_REAPED attempt=" + a.Handle + "\n"); e != nil {
			return e
		}
		if e := os.RemoveAll(filepath.Join(s.dir, "bodies", a.Handle)); e != nil {
			return e
		}
		if e := s.Update(a.Handle, func(x *Attempt) { x.Acked = true; x.BodyReaped = true; x.Response.Artifacts = nil }); e != nil {
			return e
		}
	}
	// Body directories with no attempt row are never replay authority.
	entries, e := os.ReadDir(filepath.Join(s.dir, "bodies"))
	if os.IsNotExist(e) {
		return nil
	}
	if e != nil {
		return e
	}
	if len(entries) > 1024 {
		return refusal(6, "indeterminate", "orphan body census cap")
	}
	for _, ent := range entries {
		if !hex64.MatchString(ent.Name()) {
			return refusal(6, "indeterminate", "unexpected body directory")
		}
		if _, e := s.Get(ent.Name()); e == nil {
			continue
		}
		if e := journal("W8_BODY_REAPED attempt=" + ent.Name() + "\n"); e != nil {
			return e
		}
		if e := os.RemoveAll(filepath.Join(s.dir, "bodies", ent.Name())); e != nil {
			return e
		}
	}
	return nil
}
func (s *Store) WriteBody(a *Attempt, r Response) error {
	dir := filepath.Join(s.dir, "bodies", a.Handle)
	if e := os.MkdirAll(dir, 0700); e != nil {
		return e
	}
	for name, text := range r.Artifacts {
		if filepath.Base(name) != name || name == "." || name == ".." {
			return refusal(5, "verification", "body name")
		}
		if e := atomicWrite(filepath.Join(dir, name), []byte(text), 0644); e != nil {
			return e
		}
	}
	return atomicWrite(filepath.Join(dir, "receipt.json"), r.Receipt, 0644)
}
func readStream(r io.Reader, n, max int64) ([]byte, error) {
	if n < 0 || n > max {
		return nil, refusal(2, "contract", "stream cap")
	}
	b := make([]byte, int(n))
	if _, e := io.ReadFull(r, b); e != nil {
		return nil, refusal(2, "contract", "short stream")
	}
	return b, nil
}
