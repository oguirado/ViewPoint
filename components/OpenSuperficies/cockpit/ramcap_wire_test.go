//go:build cockpit

package main

import (
	"context"
	"encoding/binary"
	"errors"
	"opensuperficies.local/srf/src/ramcap"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestRamcapStartupAndShutdownHooks(t *testing.T) {
	b, err := os.ReadFile("app.go")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	if !strings.Contains(s, "RamCapture: startRamCapture(a, ctx)") {
		t.Fatal("production RAM capture is absent from startup")
	}
	shutdown := s[strings.Index(s, "func (a *App) shutdown("):]
	abort, stop := strings.Index(shutdown, "a.grab.Abortar()"), strings.Index(shutdown, "stopRamCapture(a)")
	if abort < 0 || stop <= abort || stop > strings.Index(shutdown, "StopCooperation(a)") {
		t.Fatal("shim cleanup must follow recording abort")
	}
}

func TestNoShimMeansAbsent(t *testing.T) {
	t.Setenv("SRF_RAIZ", t.TempDir())
	a := NewApp()
	if got := startRamCapture(a, context.Background()); got != nil {
		stopRamCapture(a)
		t.Fatal("missing shim admitted capture")
	}
	ramcapLifetimes.Lock()
	_, ok := ramcapLifetimes.items[a]
	ramcapLifetimes.Unlock()
	if ok {
		t.Fatal("missing shim retained a child lifetime")
	}
}

func TestRamcapResolveContainerFromState(t *testing.T) {
	a := NewApp()
	s := &stateServer{app: a, epoch: time.Now(), source: func() (stateInput, error) { return stateTestInput(), nil }}
	stateServers.Lock()
	stateServers.items[a] = s
	stateServers.Unlock()
	t.Cleanup(func() { stateServers.Lock(); delete(stateServers.items, a); stateServers.Unlock() })
	for _, tc := range []struct {
		slot int
		want string
	}{{0, "srf-ng-0"}, {1, "srf-ng-1"}, {79, ""}, {-1, ""}} {
		got, err := resolveRamcapContainer(a, context.Background(), tc.slot)
		if got != tc.want || (err == nil) != (tc.want != "") {
			t.Fatalf("slot %d: %q %v", tc.slot, got, err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := resolveRamcapContainer(a, ctx, 0); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel ignored: %v", err)
	}
}

type ramcapMemoryStore struct {
	mu     sync.Mutex
	frames map[uint64][]byte
	head   []byte
	reaps  int
	budget int64
}

func (s *ramcapMemoryStore) Put(seq uint64, b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := int64(len(s.head))
	for _, f := range s.frames {
		n += int64(len(f))
	}
	if n+int64(len(b)) > s.budget {
		return errors.New("E_BUDGET")
	}
	s.frames[seq] = append([]byte(nil), b...)
	return nil
}
func (s *ramcapMemoryStore) Head(b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := int64(len(b))
	for _, f := range s.frames {
		n += int64(len(f))
	}
	if n > s.budget {
		return errors.New("E_BUDGET")
	}
	s.head = append([]byte(nil), b...)
	return nil
}
func (s *ramcapMemoryStore) SizeBytes() (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var n int64
	for _, b := range s.frames {
		n += int64(len(b))
	}
	return n + int64(len(s.head)), nil
}
func (s *ramcapMemoryStore) Swap() (bool, error) { return false, nil }
func (s *ramcapMemoryStore) Reap() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reaps++
	clear(s.frames)
	s.head = nil
	return nil
}

type ramcapFakeExec struct {
	mu            sync.Mutex
	calls         int
	moving, drift bool
}

func ramcapTestXWD(shade byte) []byte {
	name := []byte("srf-fbread xid=42\x00")
	f := [25]uint32{uint32(100 + len(name)), 7, 2, 24, 2, 2, 0, 0, 32, 0, 32, 32, 8, 4, 0xff0000, 0xff00, 0xff, 8, 256, 0, 2, 2, 0, 0, 0}
	b := make([]byte, 100+len(name)+16)
	for i, v := range f {
		binary.BigEndian.PutUint32(b[i*4:], v)
	}
	copy(b[100:], name)
	for i := 100 + len(name); i < len(b); i += 4 {
		b[i] = shade
		b[i+1] = shade + 1
		b[i+2] = shade + 2
	}
	return b
}
func (e *ramcapFakeExec) Run(ctx context.Context, argv []string, maxOut int64) ([]byte, []byte, int, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, nil, 5, err
	}
	e.calls++
	shade := byte(1)
	if e.moving {
		shade = byte(e.calls)
	}
	b := ramcapTestXWD(shade)
	if len(argv) < 7 {
		return nil, nil, 2, errors.New("argv")
	}
	switch argv[6] {
	case "read", "getimage":
		return b, []byte("separate diagnostics"), 0, nil
	case "pair":
		direct := b
		if e.drift {
			direct = ramcapTestXWD(shade + 1)
		}
		out := make([]byte, 0, len(b)+len(direct)+16)
		out = binary.BigEndian.AppendUint64(out, uint64(len(b)))
		out = append(out, b...)
		out = binary.BigEndian.AppendUint64(out, uint64(len(direct)))
		out = append(out, direct...)
		return out, nil, 0, nil
	}
	return nil, nil, 2, errors.New("unexpected argv")
}

func ramcapWireFixture(t *testing.T, moving bool, budget int64) (RamCapture, *ramcapMemoryStore, *ramcapFakeExec) {
	t.Helper()
	store := &ramcapMemoryStore{frames: map[uint64][]byte{}, budget: budget}
	executor := &ramcapFakeExec{moving: moving}
	cap, eng, err := wireRamcapEngine(ramcap.Config{RunDir: "fake-managed-run", Width: 2, Height: 2, Budget: budget, Cadence: 100 * time.Millisecond, Exec: executor,
		ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-1", nil },
		StoreFactory:     func(string) (ramcap.FrameStore, error) { return store, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = eng.Close() })
	return cap, store, executor
}
func TestWireAdmitLifecycleWithSharedEngine(t *testing.T) {
	cap, store, _ := ramcapWireFixture(t, false, 1<<20)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	id, err := cap.Start(ctx, 1, 0, 2000)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(320 * time.Millisecond)
	if size, err := cap.SizeBytes(ctx, id); err != nil || size <= 0 {
		t.Fatalf("size=%d err=%v", size, err)
	}
	if err := cap.Stop(ctx, id); err != nil {
		t.Fatal(err)
	}
	if err := cap.Reap(ctx, id); err != nil {
		t.Fatal(err)
	}
	if n, _ := store.SizeBytes(); n != 0 {
		t.Fatalf("retained after reap: %d", n)
	}
}
func TestMovingSceneSurvivesSizeBytesAndStop(t *testing.T) {
	cap, store, _ := ramcapWireFixture(t, true, 1<<20)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	id, err := cap.Start(ctx, 1, 0, 2000)
	if err != nil {
		t.Fatal(err)
	}
	for range 3 {
		time.Sleep(110 * time.Millisecond)
		if n, err := cap.SizeBytes(ctx, id); err != nil || n <= 0 {
			t.Fatalf("moving verify: %d %v", n, err)
		}
	}
	if err := cap.Stop(ctx, id); err != nil {
		t.Fatal(err)
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.reaps != 0 {
		t.Fatalf("moving scene reaped: %d", store.reaps)
	}
}
func TestBudgetFullKeepsFramesUntilStop(t *testing.T) {
	frame := int64(len(ramcapTestXWD(1)))
	cap, store, _ := ramcapWireFixture(t, true, 6*frame)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	id, err := cap.Start(ctx, 1, 0, 2000)
	if err != nil {
		t.Fatal(err)
	}
	// Saturate the loop budget, then continue moving verification past that boundary.
	time.Sleep(650 * time.Millisecond)
	store.mu.Lock()
	atBudget := len(store.frames)
	store.mu.Unlock()
	if atBudget < 3 {
		t.Fatalf("loop never approached budget: %d history frames", atBudget)
	}
	time.Sleep(220 * time.Millisecond)
	store.mu.Lock()
	after := len(store.frames)
	store.mu.Unlock()
	if after != atBudget {
		t.Fatalf("history did not plateau: %d -> %d", atBudget, after)
	}
	for range 5 {
		if n, err := cap.SizeBytes(ctx, id); err != nil || n <= 0 || n > 6*frame {
			t.Fatalf("budget verify: %d %v", n, err)
		}
	}
	if err := cap.Stop(ctx, id); err != nil {
		t.Fatal(err)
	}
	store.mu.Lock()
	n, reaps := len(store.frames), store.reaps
	store.mu.Unlock()
	if n == 0 || reaps != 0 {
		t.Fatalf("history lost: frames=%d reaps=%d", n, reaps)
	}
	if err := cap.Reap(ctx, id); err != nil {
		t.Fatal(err)
	}
	if n, _ := store.SizeBytes(); n != 0 {
		t.Fatalf("explicit reap left %d", n)
	}
}
func TestRamcapPairMismatchRefusedAndReaped(t *testing.T) {
	cap, store, executor := ramcapWireFixture(t, true, 1<<20)
	executor.drift = true
	id, err := cap.Start(context.Background(), 1, 0, 2000)
	if err == nil || id != "" {
		t.Fatal("mismatched paired pixels admitted")
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if len(store.frames) != 0 || len(store.head) != 0 {
		t.Fatal("refusal retained frames")
	}
}

func TestRamcapShutdownReleasesOwnerOnce(t *testing.T) {
	a := NewApp()
	calls := 0
	ramcapLifetimes.Lock()
	ramcapLifetimes.items[a] = func() { calls++ }
	ramcapLifetimes.Unlock()
	a.shutdown(context.Background())
	stopRamCapture(a)
	if calls != 1 {
		t.Fatalf("owner cleanup calls=%d", calls)
	}
}

func TestRamcapInvalidEngineConfigRefused(t *testing.T) {
	cap, eng, err := wireRamcapEngine(ramcap.Config{RunDir: "fake-managed-run", Width: -1, Height: 2, ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-1", nil }})
	if err == nil || cap != nil || eng != nil {
		if eng != nil {
			_ = eng.Close()
		}
		t.Fatal("invalid framebuffer dimensions admitted")
	}
}
