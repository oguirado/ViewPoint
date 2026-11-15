package ramcap

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"

	"opensuperficies.local/srf/src/cubook/capture"
)

const (
	DefaultBudget int64 = 256 << 20
	RunMax        int64 = 2 << 30
	HostMax       int64 = 16 << 30
	FreeMin       int64 = 8 << 30
)

// FrameStore is injectable so engine tests never write frame files to disk.
// Head is one replaceable verification workspace; Put retains the recording history.
type FrameStore interface {
	Put(uint64, []byte) error
	Head([]byte) error
	SizeBytes() (int64, error)
	Swap() (bool, error)
	Reap() error
}
type StoreOptions struct {
	Budget      int64
	CheckRunDir func(string) error
	FreeBytes   func() (int64, error)
	HostUsed    func() (int64, error)
	RunUsed     func() (int64, error)
	SwapUsed    func() (int64, error)
}
type Store struct {
	mu                            sync.Mutex
	root                          *os.Root
	run, dir                      string
	budget, used, headSize, swap0 int64
	swap, dead                    bool
	opts                          StoreOptions
}

func treeBytes(root string) (int64, error) {
	var n int64
	e := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		st, e := d.Info()
		if e != nil {
			return e
		}
		if !st.Mode().IsRegular() {
			return nil
		}
		s, ok := st.Sys().(*syscall.Stat_t)
		if !ok {
			return fail("E_RAM_QUOTA")
		}
		if s.Blocks < 0 || s.Blocks > (1<<62)/512 {
			return fail("E_RAM_QUOTA")
		}
		n += s.Blocks * 512
		return nil
	})
	return n, e
}
func swapUsed() (int64, error) {
	f, e := os.Open("/proc/meminfo")
	if e != nil {
		return 0, e
	}
	defer f.Close()
	var total, free int64
	var haveT, haveF bool
	s := bufio.NewScanner(f)
	for s.Scan() {
		p := strings.Fields(s.Text())
		if len(p) != 3 || p[2] != "kB" {
			continue
		}
		if p[0] != "SwapTotal:" && p[0] != "SwapFree:" {
			continue
		}
		v, e := strconv.ParseInt(p[1], 10, 64)
		if e != nil || v < 0 || v > (1<<62)/1024 {
			return 0, fail("E_SWAP_UNKNOWN")
		}
		if p[0] == "SwapTotal:" {
			total = v
			haveT = true
		} else {
			free = v
			haveF = true
		}
	}
	if s.Err() != nil {
		return 0, s.Err()
	}
	if !haveT || !haveF || free > total {
		return 0, fail("E_SWAP_UNKNOWN")
	}
	return (total - free) * 1024, nil
}
func OpenStore(runDir string) (*Store, error) { return OpenStoreWithOptions(runDir, StoreOptions{}) }
func OpenStoreWithOptions(runDir string, opts StoreOptions) (*Store, error) {
	if opts.CheckRunDir == nil {
		opts.CheckRunDir = capture.DefaultGuard().CheckRunDir
	}
	if e := opts.CheckRunDir(runDir); e != nil {
		return nil, e
	}
	if opts.Budget == 0 {
		opts.Budget = DefaultBudget
	}
	if opts.Budget < 0 {
		return nil, fail("E_BUDGET")
	}
	if opts.FreeBytes == nil {
		opts.FreeBytes = func() (int64, error) {
			var st syscall.Statfs_t
			if e := syscall.Statfs(runDir, &st); e != nil {
				return 0, e
			}
			if st.Bsize <= 0 || st.Bavail > uint64((1<<63-1)/st.Bsize) {
				return 0, fail("E_RAM_QUOTA")
			}
			return int64(st.Bavail) * st.Bsize, nil
		}
	}
	if opts.HostUsed == nil {
		opts.HostUsed = func() (int64, error) { return treeBytes("/dev/shm/vqa-ramws") }
	}
	if opts.RunUsed == nil {
		opts.RunUsed = func() (int64, error) { return treeBytes(runDir) }
	}
	if opts.SwapUsed == nil {
		opts.SwapUsed = swapUsed
	}
	base, e := opts.SwapUsed()
	if e != nil {
		return nil, e
	}
	var id [16]byte
	if _, e = rand.Read(id[:]); e != nil {
		return nil, e
	}
	dir := filepath.Join(runDir, "ramcap-"+hex.EncodeToString(id[:]))
	if e = os.Mkdir(dir, 0750); e != nil {
		return nil, e
	}
	root, e := os.OpenRoot(dir)
	if e != nil {
		_ = os.Remove(dir)
		return nil, e
	}
	return &Store{root: root, run: runDir, dir: dir, budget: opts.Budget, swap0: base, opts: opts}, nil
}
func (s *Store) reserve(n int64) error {
	if s.dead {
		return fail("E_NO_SESSION")
	}
	if e := s.opts.CheckRunDir(s.run); e != nil {
		return e
	}
	if n < 0 || s.used > s.budget || n > s.budget-s.used {
		return fail("E_BUDGET")
	}
	run, e := s.opts.RunUsed()
	if e != nil || run < 0 || run > RunMax || n > RunMax-run {
		return fail("E_RAM_QUOTA")
	}
	host, e := s.opts.HostUsed()
	if e != nil || host < 0 || host > HostMax || n > HostMax-host {
		return fail("E_RAM_QUOTA")
	}
	free, e := s.opts.FreeBytes()
	if e != nil || free < FreeMin || n > free-FreeMin {
		return fail("E_RAM_QUOTA")
	}
	return nil
}
func (s *Store) write(name string, b []byte, replace bool) error {
	if _, e := Validate(b, 0, 0); e != nil {
		return e
	}
	delta := int64(len(b))
	if replace {
		delta -= s.headSize
	}
	growth := max(delta, 0)
	if e := s.reserve(growth); e != nil {
		return e
	}
	flags := syscall.O_WRONLY | syscall.O_CREAT | syscall.O_EXCL | syscall.O_NOFOLLOW | syscall.O_CLOEXEC
	if replace && s.headSize > 0 {
		flags = syscall.O_WRONLY | syscall.O_TRUNC | syscall.O_NOFOLLOW | syscall.O_CLOEXEC
	}
	f, e := s.root.OpenFile(name, flags, 0640)
	if e != nil {
		return e
	}
	n, e := f.Write(b)
	ce := f.Close()
	if e != nil {
		return e
	}
	if ce != nil {
		return ce
	}
	if n != len(b) {
		return fail("E_XWD_SIZE")
	}
	s.used += delta
	if replace {
		s.headSize = int64(len(b))
	}
	return nil
}
func (s *Store) Put(seq uint64, b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.write(fmt.Sprintf("f%d.xwd", seq), b, false)
}
func (s *Store) Head(b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.write("head.xwd", b, true)
}

// ReadHead reopens the actual managed RAM file; Probe never trusts only a
// previously cached buffer when a production store can provide the bytes.
func (s *Store) ReadHead() ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.dead {
		return nil, fail("E_NO_SESSION")
	}
	if e := s.opts.CheckRunDir(s.run); e != nil {
		return nil, e
	}
	f, e := s.root.OpenFile("head.xwd", os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, s.headSize+1))
	if e != nil {
		return nil, e
	}
	if int64(len(b)) != s.headSize {
		return nil, fail("E_XWD_SIZE")
	}
	return b, nil
}
func (s *Store) SizeBytes() (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.dead {
		return 0, fail("E_NO_SESSION")
	}
	return s.used, nil
}
func (s *Store) Swap() (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n, e := s.opts.SwapUsed()
	if e != nil {
		return true, e
	}
	if n > s.swap0 {
		s.swap = true
	}
	return s.swap, nil
}
func (s *Store) Reap() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.dead {
		return nil
	}
	if e := s.opts.CheckRunDir(s.run); e != nil {
		return e
	}
	if e := os.RemoveAll(s.dir); e != nil {
		return e
	}
	if e := s.root.Close(); e != nil {
		return e
	}
	s.dead = true
	s.used = 0
	return nil
}

// RecordPointer appends only QueryPointer metadata to the managed RAM session.
func (s *Store) RecordPointer(seq uint64, x, y int, xid uint32) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	b, e := json.Marshal(struct {
		Seq uint64 `json:"seq"`
		X   int    `json:"x"`
		Y   int    `json:"y"`
		XID uint32 `json:"xid"`
	}{seq, x, y, xid})
	if e != nil {
		return e
	}
	b = append(b, '\n')
	if e = s.reserve(int64(len(b))); e != nil {
		return e
	}
	f, e := s.root.OpenFile("pointer.jsonl", os.O_WRONLY|os.O_CREATE|os.O_APPEND|syscall.O_NOFOLLOW, 0640)
	if e != nil {
		return e
	}
	n, e := f.Write(b)
	ce := f.Close()
	if e != nil {
		return e
	}
	if ce != nil {
		return ce
	}
	if n != len(b) {
		return io.ErrShortWrite
	}
	s.used += int64(n)
	return nil
}
