package switchboard

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"

	"opensuperficies.local/srf/src/workerbridge"
)

// BodyStore persists the complete, byte-identical source object before publication.
type BodyStore interface {
	Put(raw []byte) (string, error)
	Resolve(ref string) ([]byte, error)
}

func checkBody(raw []byte, ref string) error {
	if !validateRef(ref) {
		return refuse(2, "body reference grammar")
	}
	if bodyRef(raw) != ref {
		return refuse(6, "body reference digest mismatch")
	}
	if _, e := workerbridge.Parse(raw); e != nil {
		return refuse(5, "body invalid complete JSON: "+e.Error())
	}
	return nil
}

// MemBodyStore is a byte-owning, concurrency-safe test and dry-run implementation.
type MemBodyStore struct {
	mu     sync.Mutex
	bodies map[string][]byte
}

func NewMemBodyStore() *MemBodyStore { return &MemBodyStore{bodies: make(map[string][]byte)} }
func (m *MemBodyStore) Put(raw []byte) (string, error) {
	ref := bodyRef(raw)
	if e := checkBody(raw, ref); e != nil {
		return "", e
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.bodies == nil {
		m.bodies = map[string][]byte{}
	}
	if old, exists := m.bodies[ref]; exists {
		if !bytes.Equal(old, raw) {
			return "", refuse(6, "body hash collision or replacement")
		}
		return ref, nil
	}
	m.bodies[ref] = append([]byte(nil), raw...)
	return ref, nil
}
func (m *MemBodyStore) Resolve(ref string) ([]byte, error) {
	if !validateRef(ref) {
		return nil, refuse(2, "body reference grammar")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	raw, ok := m.bodies[ref]
	if !ok {
		return nil, refuse(4, "body unavailable")
	}
	out := append([]byte(nil), raw...)
	if e := checkBody(out, ref); e != nil {
		return nil, e
	}
	return out, nil
}

// DirBodyStore writes <bodies>/sha256:<digest>.json. The directory must be
// host-controlled; no capsule filesystem or guest can access this store.
type DirBodyStore struct {
	Root string
	mu   sync.Mutex
}

func NewDirBodyStore(hostBodiesDirectory string) (*DirBodyStore, error) {
	if hostBodiesDirectory == "" {
		return nil, refuse(3, "body store directory missing")
	}
	if e := os.MkdirAll(hostBodiesDirectory, 0700); e != nil {
		return nil, refuse(3, "create bodies directory: "+e.Error())
	}
	return &DirBodyStore{Root: hostBodiesDirectory}, nil
}
func (d *DirBodyStore) filename(ref string) string { return filepath.Join(d.Root, ref+".json") }
func (d *DirBodyStore) syncParent() error {
	parent, e := os.Open(d.Root)
	if e != nil {
		return e
	}
	defer parent.Close()
	return parent.Sync()
}
func (d *DirBodyStore) Put(raw []byte) (string, error) {
	ref := bodyRef(raw)
	if e := checkBody(raw, ref); e != nil {
		return "", e
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	target := d.filename(ref)
	if _, e := os.Lstat(target); e == nil {
		old, er := d.resolveFile(ref)
		if er != nil {
			return "", er
		}
		if !bytes.Equal(old, raw) {
			return "", refuse(6, "existing body is not identical")
		}
		return ref, nil
	} else if !errors.Is(e, os.ErrNotExist) {
		return "", refuse(3, "inspect body: "+e.Error())
	}
	f, e := os.CreateTemp(d.Root, ".staged-body-*") // exclusive O_EXCL staging
	if e != nil {
		return "", refuse(3, "stage body: "+e.Error())
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e != nil {
		f.Close()
		return "", refuse(3, "chmod body: "+e.Error())
	}
	n, e := f.Write(raw)
	if e != nil || n != len(raw) {
		f.Close()
		return "", refuse(3, "short body write")
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return "", refuse(3, "fsync body: "+e.Error())
	}
	if e = f.Close(); e != nil {
		return "", refuse(3, "close body: "+e.Error())
	}
	// Hardlink is an atomic no-replace publication. Never rename over an old body.
	if e = os.Link(name, target); e != nil {
		if !errors.Is(e, os.ErrExist) {
			return "", refuse(3, "publish body: "+e.Error())
		}
		old, er := d.resolveFile(ref)
		if er != nil {
			return "", er
		}
		if !bytes.Equal(old, raw) {
			return "", refuse(6, "body collision")
		}
		return ref, nil
	}
	if e = os.Remove(name); e != nil {
		return "", refuse(3, "remove body staging name: "+e.Error())
	}
	if e = d.syncParent(); e != nil {
		return "", refuse(6, "body parent fsync indeterminate: "+e.Error())
	}
	return ref, nil
}
func (d *DirBodyStore) resolveFile(ref string) ([]byte, error) {
	info, e := os.Lstat(d.filename(ref))
	if e != nil {
		return nil, refuse(4, "body file unavailable")
	}
	if !info.Mode().IsRegular() || info.Size() > 16<<20 {
		return nil, refuse(5, "body is not a bounded regular file")
	}
	f, e := os.Open(d.filename(ref))
	if e != nil {
		return nil, refuse(4, "open body: "+e.Error())
	}
	defer f.Close()
	raw, e := io.ReadAll(io.LimitReader(f, 16<<20+1))
	if e != nil {
		return nil, refuse(3, "read body: "+e.Error())
	}
	if len(raw) > 16<<20 || int64(len(raw)) != info.Size() {
		return nil, refuse(6, "body length mismatch")
	}
	if e = checkBody(raw, ref); e != nil {
		return nil, e
	}
	return raw, nil
}
func (d *DirBodyStore) Resolve(ref string) ([]byte, error) {
	if !validateRef(ref) {
		return nil, refuse(2, "body reference grammar")
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.resolveFile(ref)
}
