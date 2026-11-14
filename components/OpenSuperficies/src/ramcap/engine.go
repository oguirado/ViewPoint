package ramcap

import (
	"context"
	"crypto/sha256"
	"fmt"
	"sync"
	"time"
)

type Provenance struct {
	Source, Codec, Store string
	XID                  uint32
	Swap, Disk           bool
}
type FrameDigest struct {
	Seq          uint64
	W, H, Stride int
	Format       string
	SHA256       [32]byte
}
type Config struct {
	RunDir           string
	ResolveContainer func(context.Context, int) (string, error)
	Exec             Exec
	Width, Height    int
	Budget           int64
	Cadence          time.Duration
	StoreFactory     func(string) (FrameStore, error)
}
type Engine struct {
	mu                      sync.Mutex
	cfg                     Config
	source                  *FBSource
	live                    *session
	pending, frozen, closed bool
	generation              uint64
	bracket                 uint64
}
type session struct {
	id, ctr      string
	slot         int
	win          Window
	seq          uint64
	frame        []byte
	hash         [32]byte
	direct       FrameDigest
	pointer      *pointerSample
	store        FrameStore
	sealed, full bool
	fault        error
	cancel       context.CancelFunc
	done         chan struct{}
	seal         chan struct{}
}
type Capture struct{ e *Engine }
type Direct struct{ e *Engine }

func NewEngine(cfg Config) (*Engine, error) {
	if cfg.ResolveContainer == nil {
		return nil, fail("E_NO_CAPSULE")
	}
	if cfg.RunDir == "" {
		return nil, fail("unmanaged_run_dir")
	}
	if cfg.Budget == 0 {
		cfg.Budget = DefaultBudget
	}
	if cfg.Budget < 0 {
		return nil, fail("E_BUDGET")
	}
	if cfg.Cadence == 0 {
		cfg.Cadence = 500 * time.Millisecond
	}
	if cfg.Cadence < 100*time.Millisecond {
		return nil, fail("E_CADENCE")
	}
	source, e := NewFBSource(cfg.Exec, cfg.Width, cfg.Height)
	if e != nil {
		return nil, e
	}
	if cfg.StoreFactory == nil {
		cfg.StoreFactory = func(run string) (FrameStore, error) {
			return OpenStoreWithOptions(run, StoreOptions{Budget: cfg.Budget})
		}
	}
	return &Engine{cfg: cfg, source: source}, nil
}
func (e *Engine) Capture() *Capture { return &Capture{e} }
func (e *Engine) Direct() *Direct   { return &Direct{e} }
func (e *Engine) lookup(id string) (*session, error) {
	if e.live == nil || e.live.id != id {
		return nil, fail("E_NO_SESSION")
	}
	if e.live.fault != nil {
		return nil, e.live.fault
	}
	return e.live, nil
}
func (e *Engine) pair(ctx context.Context, s *session, xid uint32) ([]byte, Window, FrameDigest, error) {
	a, b, err := e.source.Pair(ctx, s.ctr, xid)
	if err != nil {
		return nil, Window{}, FrameDigest{}, err
	}
	h, err := Validate(a, e.cfg.Width, e.cfg.Height)
	if err != nil {
		return nil, Window{}, FrameDigest{}, err
	}
	bh, err := Validate(b, 0, 0)
	if err != nil {
		return nil, Window{}, FrameDigest{}, err
	}
	win, err := WindowInfo(b, bh)
	if err != nil {
		return nil, Window{}, FrameDigest{}, err
	}
	if xid != 0 && win.XID != xid {
		return nil, Window{}, FrameDigest{}, fail("E_XWD_GEOMETRY")
	}
	rgb, err := Crop(a, h, win.X, win.Y, win.W, win.H)
	if err != nil {
		return nil, Window{}, FrameDigest{}, err
	}
	hash, err := FrameSHA256(b, bh)
	if err != nil {
		return nil, Window{}, FrameDigest{}, err
	}
	if sha256.Sum256(rgb) != hash {
		return nil, Window{}, FrameDigest{}, fail("E_TORN")
	}
	s.pointer = e.source.takePointer()
	if s.pointer != nil && s.pointer.XID != win.XID {
		s.pointer = nil
	}
	return a, win, FrameDigest{W: win.W, H: win.H, Stride: 4 * win.W, Format: "BGRA", SHA256: hash}, nil
}
func (c *Capture) Start(ctx context.Context, slot int, xid uint32, ttl int64) (string, error) {
	e := c.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return "", fail("E_CLOSED")
	}
	if e.live != nil {
		return "", fail("E_BUSY")
	}
	if ttl <= 0 || ttl > 1800000 {
		return "", fail("E_TTL")
	}
	ctr, err := e.cfg.ResolveContainer(ctx, slot)
	if err != nil || !containerName.MatchString(ctr) {
		return "", fail("E_NO_CAPSULE")
	}
	s := &session{ctr: ctr, slot: slot, done: make(chan struct{}), seal: make(chan struct{})}
	a, win, direct, err := e.pair(ctx, s, xid)
	if err != nil {
		return "", err
	}
	if int64(len(a)) > e.cfg.Budget/2 {
		return "", fail("E_BUDGET")
	}
	store, err := e.cfg.StoreFactory(e.cfg.RunDir)
	if err != nil {
		return "", err
	}
	if store == nil {
		return "", fail("unmanaged_run_dir")
	}
	s.store = store
	if err = store.Put(1, a); err == nil {
		err = store.Head(a)
	}
	if err != nil {
		_ = store.Reap()
		return "", err
	}
	e.generation++
	s.id = fmt.Sprintf("ramcap-%d", e.generation)
	s.seq = 1
	s.frame = a
	s.win = win
	h, _ := ParseHeader(a)
	s.hash, _ = FrameSHA256(a, h)
	direct.Seq = s.seq
	s.direct = direct
	lifetime, cancel := context.WithTimeout(context.Background(), time.Duration(ttl)*time.Millisecond)
	s.cancel = cancel
	e.live = s
	e.pointer(s)
	e.frozen = e.pending
	go e.loop(lifetime, s)
	return s.id, nil
}
func (e *Engine) loop(ctx context.Context, s *session) {
	defer close(s.done)
	tick := time.NewTicker(e.cfg.Cadence)
	defer tick.Stop()
	ticks, seal := tick.C, s.seal
	for {
		select {
		case <-ctx.Done():
			e.mu.Lock()
			if e.live == s {
				if err := s.store.Reap(); err != nil {
					s.fault = err
				} else {
					e.live = nil
				}
				e.frozen = false
				e.pending = false
			}
			e.mu.Unlock()
			return
		case <-seal:
			tick.Stop()
			ticks, seal = nil, nil
		case <-ticks:
			e.mu.Lock()
			if e.live != s {
				e.mu.Unlock()
				return
			}
			if e.frozen || s.sealed || s.full || s.fault != nil {
				e.mu.Unlock()
				continue
			}
			// The read deadline is also bounded by TTL through ctx.
			b, err := e.source.Read(ctx, s.ctr)
			if err != nil {
				s.fault = err
				e.mu.Unlock()
				continue
			}
			h, err := Validate(b, e.cfg.Width, e.cfg.Height)
			if err != nil {
				s.fault = err
				e.mu.Unlock()
				continue
			}
			hash, _ := FrameSHA256(b, h)
			if hash != s.hash {
				used, err := s.store.SizeBytes()
				if err != nil {
					s.fault = err
				} else if used > e.cfg.Budget-3*int64(len(b)) {
					s.full = true
				} else if err = s.store.Put(s.seq+1, b); err != nil {
					if err.Error() == "E_BUDGET" || err.Error() == "E_RAM_QUOTA" {
						s.full = true
					} else {
						s.fault = err
					}
				} else if err = s.store.Head(b); err != nil {
					s.fault = err
				} else {
					s.seq++
					s.frame = b
					s.hash = hash
				}
			}
			e.mu.Unlock()
		}
	}
}
func (d *Direct) Freeze(ctx context.Context, display string, xid uint32) (func() error, error) {
	e := d.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return nil, fail("E_CLOSED")
	}
	if display != ":100" {
		return nil, fail("E_DISPLAY")
	}
	if e.pending || e.frozen {
		return nil, fail("E_BUSY")
	}
	e.bracket++
	bracket := e.bracket
	if e.live == nil {
		e.pending = true
	} else {
		s := e.live
		if s.fault != nil {
			return nil, s.fault
		}
		if xid != s.win.XID {
			return nil, fail("E_XWD_GEOMETRY")
		}
		a, win, direct, err := e.pair(ctx, s, xid)
		if err != nil {
			return nil, err
		}
		h, _ := ParseHeader(a)
		hash, _ := FrameSHA256(a, h)
		if hash != s.hash || win != s.win || direct.SHA256 != s.direct.SHA256 {
			if err = s.store.Head(a); err != nil {
				return nil, err
			}
			s.seq++
			s.frame = a
			s.hash = hash
		}
		s.win = win
		direct.Seq = s.seq
		s.direct = direct
		e.pointer(s)
		e.frozen = true
	}
	var once sync.Once
	var releaseErr error
	release := func() error {
		once.Do(func() {
			e.mu.Lock()
			defer e.mu.Unlock()
			if e.bracket != bracket {
				return
			}
			// Pair rc=0 proves the bounded child's deferred UngrabServer succeeded.
			// Never re-read after ungrab: that would compare different moving instants.
			if e.live != nil && e.live.fault != nil {
				releaseErr = e.live.fault
			}
			e.pending = false
			e.frozen = false
		})
		return releaseErr
	}
	return release, nil
}
func (d *Direct) Read(ctx context.Context, display string, xid uint32) (FrameDigest, error) {
	e := d.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return FrameDigest{}, err
	}
	if display != ":100" || !e.frozen || e.live == nil {
		return FrameDigest{}, fail("not frozen")
	}
	if xid != e.live.win.XID {
		return FrameDigest{}, fail("E_XWD_GEOMETRY")
	}
	return e.live.direct, nil
}
func (c *Capture) Provenance(ctx context.Context, id string) (Provenance, error) {
	e := c.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return Provenance{}, err
	}
	s, err := e.lookup(id)
	if err != nil {
		return Provenance{}, err
	}
	swap, err := s.store.Swap()
	if err != nil {
		return Provenance{}, err
	}
	return Provenance{Source: "x11-framebuffer", Codec: "raw", Store: "vadapt.ramws", XID: s.win.XID, Swap: swap}, nil
}
func (c *Capture) Probe(ctx context.Context, id, display string, xid uint32) (FrameDigest, error) {
	e := c.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return FrameDigest{}, err
	}
	s, err := e.lookup(id)
	if err != nil {
		return FrameDigest{}, err
	}
	if display != ":100" || xid != s.win.XID || !e.frozen {
		return FrameDigest{}, fail("not frozen")
	}
	frame := s.frame
	if reader, ok := s.store.(interface{ ReadHead() ([]byte, error) }); ok {
		frame, err = reader.ReadHead()
		if err != nil {
			return FrameDigest{}, err
		}
	}
	h, err := Validate(frame, 0, 0)
	if err != nil {
		return FrameDigest{}, err
	}
	rgb, err := Crop(frame, h, s.win.X, s.win.Y, s.win.W, s.win.H)
	if err != nil {
		return FrameDigest{}, err
	}
	return FrameDigest{Seq: s.seq, W: s.win.W, H: s.win.H, Stride: 4 * s.win.W, Format: "BGRA", SHA256: sha256.Sum256(rgb)}, nil
}
func (c *Capture) SizeBytes(ctx context.Context, id string) (int64, error) {
	e := c.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	s, err := e.lookup(id)
	if err != nil {
		return 0, err
	}
	return s.store.SizeBytes()
}
func (c *Capture) Stop(ctx context.Context, id string) error {
	e := c.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	s, err := e.lookup(id)
	if err != nil {
		return err
	}
	if !s.sealed {
		s.sealed = true
		close(s.seal)
	}
	return nil
}

// Reap accepts empty and unknown IDs without removing another live session.
func (c *Capture) Reap(ctx context.Context, id string) error {
	e := c.e
	e.mu.Lock()
	s := e.live
	if s == nil || s.id != id {
		e.mu.Unlock()
		return nil
	}
	s.cancel()
	err := s.store.Reap()
	if err == nil {
		e.live = nil
	} else {
		s.fault = err
	}
	e.frozen = false
	e.pending = false
	e.mu.Unlock()
	select {
	case <-s.done:
		return err
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(2 * time.Second):
		return fail("E_TIMEOUT")
	}
}
func (e *Engine) Close() error {
	e.mu.Lock()
	e.closed = true
	id := ""
	if e.live != nil {
		id = e.live.id
	}
	e.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return e.Capture().Reap(ctx, id)
}

// Optional metadata is quota-bound and never changes framebuffer pixels. At full
// metadata quota the recording remains available with its already-retained trail.
func (e *Engine) pointer(s *session) {
	if s.pointer == nil {
		return
	}
	if sink, ok := s.store.(interface {
		RecordPointer(uint64, int, int, uint32) error
	}); ok {
		if err := sink.RecordPointer(s.seq, s.pointer.X, s.pointer.Y, s.pointer.XID); err != nil && err.Error() != "E_BUDGET" && err.Error() != "E_RAM_QUOTA" {
			s.fault = err
		}
	}
}
