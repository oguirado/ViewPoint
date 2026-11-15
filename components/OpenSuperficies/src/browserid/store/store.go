// Package store keeps browser identity metadata and one exclusive, durable holder
// per identity. Profile payloads are opaque to this package.
package store

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode"
	"unicode/utf8"
)

var (
	ErrInvalid       = errors.New("invalid browser identity")
	ErrExists        = errors.New("identity already exists")
	ErrNotFound      = errors.New("identity not found")
	ErrCorrupt       = errors.New("corrupt identity store record")
	ErrLeaseMismatch = errors.New("lease token or counter mismatch")
	ErrNotHeld       = errors.New("identity is not held")

	profileIDRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,47}$`)
	holderIDRE  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)
	labelRE     = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
	// A conservative, documented subset of multi-label public suffixes. This
	// cannot substitute for an up-to-date Public Suffix List.
	compoundSuffixes = map[string]bool{
		"ac.uk": true, "co.uk": true, "gov.uk": true, "ltd.uk": true,
		"me.uk": true, "net.uk": true, "org.uk": true, "plc.uk": true,
		"com.au": true, "net.au": true, "org.au": true, "edu.au": true,
		"gov.au": true, "co.jp": true, "ne.jp": true, "or.jp": true,
		"co.nz": true, "org.nz": true, "net.nz": true, "com.br": true,
		"com.mx": true, "co.in": true, "co.kr": true, "co.za": true,
		"com.cn": true, "com.hk": true, "com.tw": true, "com.sg": true,
		"com.tr": true, "com.ar": true, "com.co": true, "com.pl": true,
		"com.ua": true, "com.my": true, "com.ph": true, "com.pk": true,
	}
)

const maxRecordBytes = 64 * 1024

// Identity is public metadata only: never include profile contents, cookies or
// credentials. JSON field names are part of the persistent contract.
type Identity struct {
	ProfileID      string   `json:"profile_id"`
	Browser        string   `json:"browser"`
	AccountLabel   string   `json:"account_label"`
	AllowedDomains []string `json:"allowed_domains"`
	CreatedUnix    int64    `json:"created_unix"`
	LastLoginUnix  *int64   `json:"last_login_unix"`
}

// Lease is an opaque authorization for Release. Token must never be logged.
type Lease struct {
	ProfileID    string
	Holder       string
	Counter      uint64
	AcquiredUnix int64
	Token        string
}

// BusyError exposes only the holder identifier, never any filesystem path.
type BusyError struct{ HolderID string }

func (e *BusyError) Error() string { return "identity busy; holder=" + e.HolderID }

// filePort makes all filesystem opens visible to the package's opaque-profile
// tests; the store never opens or enumerates anything under profiles/<id>/.
type filePort interface {
	MkdirAll(string, os.FileMode) error
	Mkdir(string, os.FileMode) error
	Lstat(string) (os.FileInfo, error)
	OpenFile(string, int, os.FileMode) (*os.File, error)
	Rename(string, string) error
	Remove(string) error
	Chmod(string, os.FileMode) error
}

type diskFiles struct{}

func (diskFiles) MkdirAll(p string, m os.FileMode) error { return os.MkdirAll(p, m) }
func (diskFiles) Mkdir(p string, m os.FileMode) error    { return os.Mkdir(p, m) }
func (diskFiles) Lstat(p string) (os.FileInfo, error)    { return os.Lstat(p) }
func (diskFiles) OpenFile(p string, f int, m os.FileMode) (*os.File, error) {
	return os.OpenFile(p, f, m)
}
func (diskFiles) Rename(a, b string) error            { return os.Rename(a, b) }
func (diskFiles) Remove(p string) error               { return os.Remove(p) }
func (diskFiles) Chmod(p string, m os.FileMode) error { return os.Chmod(p, m) }

// Store is safe for concurrent goroutines and processes on a local filesystem
// supporting Linux flock, exclusive creation, atomic rename and O_APPEND.
type Store struct {
	root string
	fs   filePort
	now  func() time.Time
}

// New initializes a private store rooted at root. Existing symlinked store
// directories and non-directories are refused.
func New(root string) (*Store, error) { return newWithPort(root, diskFiles{}, time.Now) }

func newWithPort(root string, fs filePort, now func() time.Time) (*Store, error) {
	if root == "" || fs == nil || now == nil {
		return nil, ErrInvalid
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	s := &Store{root: abs, fs: fs, now: now}
	if err := fs.MkdirAll(abs, 0o700); err != nil {
		return nil, err
	}
	for _, p := range []string{abs, s.metadataDir(), s.profileRoot(), s.lockDir()} {
		if err := s.privateDir(p); err != nil {
			return nil, err
		}
	}
	return s, nil
}

func (s *Store) metadataDir() string           { return filepath.Join(s.root, "metadata") }
func (s *Store) profileRoot() string           { return filepath.Join(s.root, "profiles") }
func (s *Store) lockDir() string               { return filepath.Join(s.root, "locks") }
func (s *Store) metadataPath(id string) string { return filepath.Join(s.metadataDir(), id+".json") }
func (s *Store) lockPath(id string) string     { return filepath.Join(s.lockDir(), id+".json") }

func (s *Store) privateDir(p string) error {
	fi, err := s.fs.Lstat(p)
	if errors.Is(err, os.ErrNotExist) {
		if err = s.fs.Mkdir(p, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
			return err
		}
		fi, err = s.fs.Lstat(p)
	}
	if err != nil {
		return err
	}
	if !fi.IsDir() || fi.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("store directory must be a real directory")
	}
	return s.fs.Chmod(p, 0o700)
}

func validateID(id string) error {
	if !profileIDRE.MatchString(id) {
		return fmt.Errorf("%w: profile_id", ErrInvalid)
	}
	return nil
}

func validateHolder(holder string) error {
	if !holderIDRE.MatchString(holder) {
		return fmt.Errorf("%w: holder", ErrInvalid)
	}
	return nil
}

// validateDomain enforces exact registrable-domain shape conservatively. The
// supported compound public suffix set is fixed; no network/PSL lookup occurs.
func validateDomain(d string) bool {
	if len(d) > 253 || d != strings.ToLower(d) {
		return false
	}
	labels := strings.Split(d, ".")
	if len(labels) < 2 || len(labels) > 3 {
		return false
	}
	for _, l := range labels {
		if !labelRE.MatchString(l) {
			return false
		}
	}
	tld := labels[len(labels)-1]
	if len(tld) < 2 || len(tld) > 24 {
		return false
	}
	for _, c := range tld {
		if c < 'a' || c > 'z' {
			return false
		}
	}
	if len(labels) == 2 {
		return !compoundSuffixes[d]
	}
	return compoundSuffixes[strings.Join(labels[1:], ".")]
}

func validateIdentity(id Identity) error {
	if err := validateID(id.ProfileID); err != nil {
		return err
	}
	if id.Browser != "chromium" && id.Browser != "firefox" && id.Browser != "program" {
		return fmt.Errorf("%w: browser", ErrInvalid)
	}
	if !utf8.ValidString(id.AccountLabel) || len(id.AccountLabel) == 0 || len(id.AccountLabel) > 128 || strings.TrimSpace(id.AccountLabel) != id.AccountLabel {
		return fmt.Errorf("%w: account_label", ErrInvalid)
	}
	for _, r := range id.AccountLabel {
		if unicode.IsControl(r) {
			return fmt.Errorf("%w: account_label", ErrInvalid)
		}
	}
	if len(id.AllowedDomains) < 1 || len(id.AllowedDomains) > 16 || !sort.StringsAreSorted(id.AllowedDomains) {
		return fmt.Errorf("%w: allowed_domains", ErrInvalid)
	}
	for i, d := range id.AllowedDomains {
		if !validateDomain(d) || (i > 0 && d == id.AllowedDomains[i-1]) {
			return fmt.Errorf("%w: allowed_domains", ErrInvalid)
		}
	}
	if id.CreatedUnix <= 0 || (id.LastLoginUnix != nil && *id.LastLoginUnix < id.CreatedUnix) {
		return fmt.Errorf("%w: timestamps", ErrInvalid)
	}
	return nil
}

// readRecord refuses links, directories, excessive sizes, unknown JSON fields
// and trailing data. Never call this with a profile payload path.
func (s *Store) readRecord(p string, out any) error {
	fi, err := s.fs.Lstat(p)
	if err != nil {
		return err
	}
	if !fi.Mode().IsRegular() || fi.Size() > maxRecordBytes {
		return ErrCorrupt
	}
	f, err := s.fs.OpenFile(p, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	r := io.LimitReader(f, maxRecordBytes+1)
	dec := json.NewDecoder(r)
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		return ErrCorrupt
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return ErrCorrupt
	}
	return nil
}

func (s *Store) readIdentity(id string) (Identity, error) {
	var record Identity
	if err := s.readRecord(s.metadataPath(id), &record); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return record, ErrNotFound
		}
		return record, fmt.Errorf("%w: metadata unreadable", ErrCorrupt)
	}
	if record.ProfileID != id || validateIdentity(record) != nil {
		return Identity{}, ErrCorrupt
	}
	return record, nil
}

// Get retrieves only validated public metadata, never a profile payload.
func (s *Store) Get(profileID string) (Identity, error) {
	if err := validateID(profileID); err != nil {
		return Identity{}, err
	}
	return s.readIdentity(profileID)
}

func (s *Store) atomicWrite(dir, dst string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	if len(b) > maxRecordBytes {
		return ErrInvalid
	}
	var entropy [12]byte
	if _, err = rand.Read(entropy[:]); err != nil {
		return err
	}
	p := filepath.Join(dir, "."+filepath.Base(dst)+"-"+hex.EncodeToString(entropy[:])+".tmp")
	f, err := s.fs.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	defer s.fs.Remove(p)
	if err = s.fs.Chmod(p, 0o600); err != nil {
		f.Close()
		return err
	}
	if _, err = f.Write(b); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return s.fs.Rename(p, dst)
}

func (s *Store) appendAudit(action, id, holder string) error {
	b, err := json.Marshal(struct {
		Time      int64  `json:"time"`
		Action    string `json:"action"`
		ProfileID string `json:"profile_id"`
		Holder    string `json:"holder"`
		Result    string `json:"result"`
	}{s.now().Unix(), action, id, holder, "ok"})
	if err != nil {
		return err
	}
	f, err := s.fs.OpenFile(filepath.Join(s.root, "audit.jsonl"), os.O_WRONLY|os.O_CREATE|os.O_APPEND|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if err = f.Chmod(0o600); err != nil {
		return err
	}
	b = append(b, '\n')
	n, err := f.Write(b)
	if err != nil {
		return err
	}
	if n != len(b) {
		return io.ErrShortWrite
	}
	return f.Sync()
}

// withGuard serializes mutations across independent processes. Guard files
// are not leases; flock unlocks automatically after process termination.
func (s *Store) withGuard(id string, fn func() error) error {
	p := filepath.Join(s.lockDir(), id+".guard")
	f, err := s.fs.OpenFile(p, os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if err = f.Chmod(0o600); err != nil {
		return err
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	return fn()
}

// Create installs one identity and its private profile directory. CreatedUnix
// defaults to the injected clock if zero. Duplicate IDs are never overwritten.
func (s *Store) Create(id Identity) error {
	if id.CreatedUnix == 0 {
		id.CreatedUnix = s.now().Unix()
	}
	if err := validateIdentity(id); err != nil {
		return err
	}
	return s.withGuard(id.ProfileID, func() error {
		_, err := s.fs.Lstat(s.metadataPath(id.ProfileID))
		if err == nil {
			return ErrExists
		}
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		profileDir := filepath.Join(s.profileRoot(), id.ProfileID)
		// Never adopt an orphaned profile directory: it could already hold
		// opaque credentials from an interrupted or unrelated creation.
		if err := s.fs.Mkdir(profileDir, 0o700); err != nil {
			if errors.Is(err, os.ErrExist) {
				return ErrExists
			}
			return err
		}
		if err := s.fs.Chmod(profileDir, 0o700); err != nil {
			return err
		}
		if err := s.atomicWrite(s.metadataDir(), s.metadataPath(id.ProfileID), id); err != nil {
			return err
		}
		return s.appendAudit("create", id.ProfileID, "")
	})
}

// RecordLogin updates last_login_unix from a trusted operator signal; it does
// not inspect browser data or infer login from files in the profile directory.
func (s *Store) RecordLogin(profileID string) error {
	if err := validateID(profileID); err != nil {
		return err
	}
	return s.withGuard(profileID, func() error {
		id, err := s.readIdentity(profileID)
		if err != nil {
			return err
		}
		now := s.now().Unix()
		if now < id.CreatedUnix {
			return fmt.Errorf("%w: login time predates creation", ErrInvalid)
		}
		id.LastLoginUnix = &now
		if err := s.atomicWrite(s.metadataDir(), s.metadataPath(profileID), id); err != nil {
			return err
		}
		return s.appendAudit("login", profileID, "")
	})
}

type lockRecord struct {
	Holder       string `json:"holder"`
	Counter      uint64 `json:"acquire_counter"`
	AcquiredUnix int64  `json:"acquired_unix"`
	TokenSHA256  string `json:"token_sha256"`
	Active       bool   `json:"active"`
}

func (s *Store) readLock(id string) (lockRecord, error) {
	var r lockRecord
	if err := s.readRecord(s.lockPath(id), &r); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return r, nil
		}
		return r, ErrCorrupt
	}
	if r.Counter == 0 || r.AcquiredUnix <= 0 || validateHolder(r.Holder) != nil {
		return r, ErrCorrupt
	}
	if r.Active {
		if len(r.TokenSHA256) != 64 {
			return r, ErrCorrupt
		}
		if _, err := hex.DecodeString(r.TokenSHA256); err != nil {
			return r, ErrCorrupt
		}
	} else if r.TokenSHA256 != "" {
		return r, ErrCorrupt
	}
	return r, nil
}

// Acquire fails closed if an identity is already held, regardless of its age.
// Only Reclaim can explicitly clear an abandoned live lease.
func (s *Store) Acquire(profileID, holder string) (Lease, error) {
	var lease Lease
	if err := validateID(profileID); err != nil {
		return lease, err
	}
	if err := validateHolder(holder); err != nil {
		return lease, err
	}
	err := s.withGuard(profileID, func() error {
		if _, err := s.readIdentity(profileID); err != nil {
			return err
		}
		r, err := s.readLock(profileID)
		if err != nil {
			return err
		}
		if r.Active {
			return &BusyError{HolderID: r.Holder}
		}
		if r.Counter == math.MaxUint64 {
			return ErrCorrupt
		}
		var tokenBytes [32]byte
		if _, err := rand.Read(tokenBytes[:]); err != nil {
			return err
		}
		lease = Lease{ProfileID: profileID, Holder: holder, Counter: r.Counter + 1, AcquiredUnix: s.now().Unix(), Token: hex.EncodeToString(tokenBytes[:])}
		digest := sha256.Sum256([]byte(lease.Token))
		r = lockRecord{Holder: holder, Counter: lease.Counter, AcquiredUnix: lease.AcquiredUnix, TokenSHA256: hex.EncodeToString(digest[:]), Active: true}
		if err := s.atomicWrite(s.lockDir(), s.lockPath(profileID), r); err != nil {
			return err
		}
		return s.appendAudit("acquire", profileID, holder)
	})
	if err != nil {
		return Lease{}, err
	}
	return lease, nil
}

// Release requires exactly the active lease's profile, holder, counter and
// token. A stale or altered lease can never release a new holder.
func (s *Store) Release(lease Lease) error {
	if err := validateID(lease.ProfileID); err != nil {
		return err
	}
	if err := validateHolder(lease.Holder); err != nil {
		return err
	}
	return s.withGuard(lease.ProfileID, func() error {
		if _, err := s.readIdentity(lease.ProfileID); err != nil {
			return err
		}
		r, err := s.readLock(lease.ProfileID)
		if err != nil {
			return err
		}
		if !r.Active {
			return ErrNotHeld
		}
		digest := sha256.Sum256([]byte(lease.Token))
		want, err := hex.DecodeString(r.TokenSHA256)
		if err != nil {
			return ErrCorrupt
		}
		if r.Holder != lease.Holder || r.Counter != lease.Counter || subtle.ConstantTimeCompare(digest[:], want) != 1 {
			return ErrLeaseMismatch
		}
		r.Active = false
		r.TokenSHA256 = ""
		if err := s.atomicWrite(s.lockDir(), s.lockPath(lease.ProfileID), r); err != nil {
			return err
		}
		return s.appendAudit("release", lease.ProfileID, lease.Holder)
	})
}

// Reclaim is an explicit operator-controlled stale-lease override. The caller
// must supply the observed acquire counter as a guard against newer leases.
func (s *Store) Reclaim(profileID string, expectedCounter uint64) error {
	if err := validateID(profileID); err != nil {
		return err
	}
	return s.withGuard(profileID, func() error {
		if _, err := s.readIdentity(profileID); err != nil {
			return err
		}
		r, err := s.readLock(profileID)
		if err != nil {
			return err
		}
		if !r.Active {
			return ErrNotHeld
		}
		if r.Counter != expectedCounter {
			return ErrLeaseMismatch
		}
		r.Active = false
		r.TokenSHA256 = ""
		if err := s.atomicWrite(s.lockDir(), s.lockPath(profileID), r); err != nil {
			return err
		}
		return s.appendAudit("reclaim", profileID, r.Holder)
	})
}
