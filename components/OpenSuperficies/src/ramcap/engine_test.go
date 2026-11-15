package ramcap

import (
	"context"
	"encoding/binary"
	"sync"
	"testing"
	"time"
)

type memoryStore struct {
	mu      sync.Mutex
	frames  map[uint64][]byte
	head    []byte
	reaps   int
	swapped bool
}

func (s *memoryStore) Put(seq uint64, b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.frames == nil {
		s.frames = map[uint64][]byte{}
	}
	if s.frames[seq] != nil {
		return fail("duplicate")
	}
	s.frames[seq] = append([]byte{}, b...)
	return nil
}
func (s *memoryStore) Head(b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.head = append([]byte{}, b...)
	return nil
}
func (s *memoryStore) SizeBytes() (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := len(s.head)
	for _, b := range s.frames {
		n += len(b)
	}
	return int64(n), nil
}
func (s *memoryStore) Swap() (bool, error) { s.mu.Lock(); defer s.mu.Unlock(); return s.swapped, nil }
func (s *memoryStore) Reap() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.frames = nil
	s.head = nil
	s.reaps++
	return nil
}
func testEngine(t *testing.T, f *movingExec, budget int64) (*Engine, *memoryStore) {
	t.Helper()
	store := &memoryStore{}
	e, err := NewEngine(Config{RunDir: "fake-managed-run", ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-1", nil }, Exec: f, Width: 8, Height: 4, Budget: budget, Cadence: 100 * time.Millisecond, StoreFactory: func(string) (FrameStore, error) { return store, nil }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if e := e.Close(); e != nil {
			t.Error(e)
		}
	})
	return e, store
}
func TestEngineSingleSession(t *testing.T) {
	e, _ := testEngine(t, &movingExec{}, 0)
	id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.Capture().Start(context.Background(), 2, 0, 10000)
	code(t, err, "E_BUSY")
	if err = e.Capture().Reap(context.Background(), id); err != nil {
		t.Fatal(err)
	}
}
func TestEngineFreezeBeforeStart(t *testing.T) {
	e, _ := testEngine(t, &movingExec{}, 0)
	release, err := e.Direct().Freeze(context.Background(), ":100", 0)
	if err != nil {
		t.Fatal(err)
	}
	id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	if err != nil {
		t.Fatal(err)
	}
	p, err := e.Capture().Provenance(context.Background(), id)
	if err != nil || p.XID != 42 {
		t.Fatal(p, err)
	}
	held, err := e.Capture().Probe(context.Background(), id, ":100", 42)
	if err != nil {
		t.Fatal(err)
	}
	direct, err := e.Direct().Read(context.Background(), ":100", 42)
	if err != nil || held != direct {
		t.Fatal(held, direct, err)
	}
	if err = release(); err != nil {
		t.Fatal(err)
	}
	_, err = e.Direct().Read(context.Background(), ":100", 42)
	code(t, err, "not frozen")
	_, err = e.Direct().Freeze(context.Background(), ":100", 43)
	code(t, err, "E_XWD_GEOMETRY")
}
func TestMovingSceneSurvivesSizeBytesAndStop(t *testing.T) {
	e, s := testEngine(t, &movingExec{moving: true}, 0)
	rel, err := e.Direct().Freeze(context.Background(), ":100", 0)
	if err != nil {
		t.Fatal(err)
	}
	id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	if err != nil {
		t.Fatal(err)
	}
	if err = rel(); err != nil {
		t.Fatal(err)
	}
	var seq uint64
	for i := range 4 {
		rel, err = e.Direct().Freeze(context.Background(), ":100", 42)
		if err != nil {
			t.Fatal(err)
		}
		a, err := e.Capture().Probe(context.Background(), id, ":100", 42)
		if err != nil {
			t.Fatal(err)
		}
		b, err := e.Direct().Read(context.Background(), ":100", 42)
		if err != nil || a != b || a.Seq <= seq {
			t.Fatal(a, b, err)
		}
		seq = a.Seq
		if i < 3 {
			if n, err := e.Capture().SizeBytes(context.Background(), id); err != nil || n == 0 {
				t.Fatal(n, err)
			}
		} else if err = e.Capture().Stop(context.Background(), id); err != nil {
			t.Fatal(err)
		}
		if err = rel(); err != nil {
			t.Fatal(err)
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.reaps != 0 {
		t.Fatal(s.reaps)
	}
}
func TestBudgetFullKeepsFramesUntilStop(t *testing.T) {
	f := &movingExec{moving: true}
	size := int64(len(buildXWD(8, 4, nil, "srf-fbread xid=42\x00")))
	e, s := testEngine(t, f, size*5)
	id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.After(2 * time.Second)
	for {
		e.mu.Lock()
		full := e.live != nil && e.live.full
		e.mu.Unlock()
		if full {
			break
		}
		select {
		case <-deadline:
			t.Fatal("budget never saturated")
		case <-time.After(10 * time.Millisecond):
		}
	}
	before, _ := e.Capture().SizeBytes(context.Background(), id)
	for range 6 {
		rel, err := e.Direct().Freeze(context.Background(), ":100", 42)
		if err != nil {
			t.Fatal(err)
		}
		if err = rel(); err != nil {
			t.Fatal(err)
		}
	}
	after, _ := e.Capture().SizeBytes(context.Background(), id)
	if before != after || after > size*5 || after == 0 {
		t.Fatal(before, after)
	}
	if err = e.Capture().Stop(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.frames) == 0 || s.reaps != 0 {
		t.Fatal("lost retained frames")
	}
}
func TestReapUnknownId(t *testing.T) {
	e, s := testEngine(t, &movingExec{}, 0)
	id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	if err != nil {
		t.Fatal(err)
	}
	if err = e.Capture().Reap(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if err = e.Capture().Reap(context.Background(), "unknown"); err != nil {
		t.Fatal(err)
	}
	if n, err := e.Capture().SizeBytes(context.Background(), id); err != nil || n == 0 {
		t.Fatal(n, err)
	}
	if err = e.Capture().Reap(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.reaps != 1 {
		t.Fatal(s.reaps)
	}
}
func TestTTLReaps(t *testing.T) {
	e, s := testEngine(t, &movingExec{}, 0)
	_, err := e.Capture().Start(context.Background(), 1, 0, 30)
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.After(time.Second)
	for {
		s.mu.Lock()
		n := s.reaps
		s.mu.Unlock()
		if n == 1 {
			return
		}
		select {
		case <-deadline:
			t.Fatal("TTL did not reap")
		case <-time.After(5 * time.Millisecond):
		}
	}
}
func TestPairMismatchRefused(t *testing.T) {
	e, s := testEngine(t, &movingExec{mismatch: true}, 0)
	_, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	code(t, err, "E_TORN")
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.frames) != 0 {
		t.Fatal("stored mismatched pair")
	}
}

func TestXidZeroResolvesRootAndSubwindowCrop(t *testing.T) {
	for _, offscreen := range []bool{false, true} {
		t.Run(map[bool]string{false: "subwindow", true: "offscreen"}[offscreen], func(t *testing.T) {
			store := &memoryStore{}
			ex := execFunc(func(ctx context.Context, a []string, _ int64) ([]byte, []byte, int, error) {
				root := buildXWD(8, 4, nil, "")
				rh, _ := Validate(root, 0, 0)
				sub := buildXWD(3, 2, map[int]uint32{22: 2, 23: 1}, "srf-fbread xid=42\x00")
				sh, _ := Validate(sub, 0, 0)
				for y := range 2 {
					copy(sub[int(sh.PixelsOffset)+y*12:], root[int(rh.PixelsOffset)+(y+1)*rh.BytesPerLine+2*4:int(rh.PixelsOffset)+(y+1)*rh.BytesPerLine+5*4])
				}
				if offscreen {
					binary.BigEndian.PutUint32(sub[22*4:], 0xffffffff)
				}
				return pairBytes(root, sub), nil, 0, nil
			})
			e, err := NewEngine(Config{RunDir: "fake", ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-1", nil }, Exec: ex, Width: 8, Height: 4, StoreFactory: func(string) (FrameStore, error) { return store, nil }})
			if err != nil {
				t.Fatal(err)
			}
			defer e.Close()
			release, err := e.Direct().Freeze(context.Background(), ":100", 0)
			if err != nil {
				t.Fatal(err)
			}
			defer release()
			id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
			if offscreen {
				code(t, err, "E_XWD_GEOMETRY")
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			p, err := e.Capture().Provenance(context.Background(), id)
			if err != nil || p.XID != 42 {
				t.Fatal(p, err)
			}
			a, err := e.Capture().Probe(context.Background(), id, ":100", 42)
			if err != nil {
				t.Fatal(err)
			}
			b, err := e.Direct().Read(context.Background(), ":100", 42)
			if err != nil || a != b || a.W != 3 || a.H != 2 {
				t.Fatal(a, b, err)
			}
		})
	}
}

func (s *memoryStore) ReadHead() ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]byte{}, s.head...), nil
}
func TestStoredFrameDriftIsVisibleToAdmission(t *testing.T) {
	e, s := testEngine(t, &movingExec{}, 0)
	rel, err := e.Direct().Freeze(context.Background(), ":100", 0)
	if err != nil {
		t.Fatal(err)
	}
	defer rel()
	id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	if err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.head[len(s.head)-4]++
	s.mu.Unlock()
	held, err := e.Capture().Probe(context.Background(), id, ":100", 42)
	if err != nil {
		t.Fatal(err)
	}
	direct, err := e.Direct().Read(context.Background(), ":100", 42)
	if err != nil {
		t.Fatal(err)
	}
	if held == direct {
		t.Fatal("Probe ignored actual stored bytes")
	}
}

type budgetRefusalStore struct{ memoryStore }

func (s *budgetRefusalStore) Put(uint64, []byte) error { return fail("E_BUDGET") }
func TestBudgetReaper(t *testing.T) {
	s := &budgetRefusalStore{}
	e, err := NewEngine(Config{RunDir: "fake", ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-1", nil }, Exec: &movingExec{}, Width: 8, Height: 4, StoreFactory: func(string) (FrameStore, error) { return s, nil }})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	_, err = e.Capture().Start(context.Background(), 1, 0, 10000)
	code(t, err, "E_BUDGET")
	if s.reaps != 1 {
		t.Fatal("failed start workspace not reaped")
	}
}

type pointerStore struct {
	memoryStore
	samples []struct {
		seq  uint64
		x, y int
		xid  uint32
	}
}

func (s *pointerStore) RecordPointer(seq uint64, x, y int, xid uint32) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.samples = append(s.samples, struct {
		seq  uint64
		x, y int
		xid  uint32
	}{seq, x, y, xid})
	return nil
}
func TestPointerMetadataNeverDrawn(t *testing.T) {
	store := &pointerStore{}
	frame := buildXWD(8, 4, nil, "srf-fbread xid=42\x00")
	ex := execFunc(func(context.Context, []string, int64) ([]byte, []byte, int, error) {
		return pairBytes(frame, frame), []byte("{\"x\":7,\"y\":3,\"xid\":42}\n"), 0, nil
	})
	e, err := NewEngine(Config{RunDir: "fake", ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-1", nil }, Exec: ex, Width: 8, Height: 4, StoreFactory: func(string) (FrameStore, error) { return store, nil }})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	rel, err := e.Direct().Freeze(context.Background(), ":100", 0)
	if err != nil {
		t.Fatal(err)
	}
	defer rel()
	id, err := e.Capture().Start(context.Background(), 1, 0, 10000)
	if err != nil {
		t.Fatal(err)
	}
	got, err := e.Capture().Probe(context.Background(), id, ":100", 42)
	if err != nil {
		t.Fatal(err)
	}
	h, _ := Validate(frame, 0, 0)
	want, _ := FrameSHA256(frame, h)
	if got.SHA256 != want {
		t.Fatal("pointer changed pixels")
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if len(store.samples) != 1 || store.samples[0].seq != 1 || store.samples[0].x != 7 || store.samples[0].y != 3 || store.samples[0].xid != 42 {
		t.Fatal(store.samples)
	}
}
