package inject_test

import (
	"context"
	"errors"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"opensuperficies.local/srf/src/browserid/inject"
)

type fakeTiling struct {
	mu         sync.Mutex
	placements map[string]inject.Placement
	resolves   int
	changeAt   int
	changed    inject.Placement
}

func (f *fakeTiling) Resolve(surfaceID string) (inject.Placement, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.resolves++
	if f.changeAt > 0 && f.resolves >= f.changeAt {
		return f.changed, nil
	}
	p, ok := f.placements[surfaceID]
	if !ok {
		return inject.Placement{}, errors.New("missing")
	}
	return p, nil
}

type fakeExecutor struct {
	mu            sync.Mutex
	focus         map[string]string
	events        map[string][]string
	forceFocus    string
	keyHook       func(display string)
	activationErr error
	focusErr      error
	inputErr      error
	lastClick     [3]int
}

func newExecutor() *fakeExecutor {
	return &fakeExecutor{focus: map[string]string{}, events: map[string][]string{}}
}

func (f *fakeExecutor) append(display, event string) {
	f.mu.Lock()
	f.events[display] = append(f.events[display], event)
	f.mu.Unlock()
}

func (f *fakeExecutor) Activate(_ context.Context, displayID, windowID string) error {
	f.mu.Lock()
	f.focus[displayID] = windowID
	f.events[displayID] = append(f.events[displayID], "activate:"+windowID)
	f.mu.Unlock()
	return f.activationErr
}
func (f *fakeExecutor) Focused(_ context.Context, displayID string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events[displayID] = append(f.events[displayID], "focus")
	if f.forceFocus != "" {
		return f.forceFocus, f.focusErr
	}
	return f.focus[displayID], f.focusErr
}
func (f *fakeExecutor) Key(_ context.Context, displayID, sym string) error {
	f.append(displayID, "key:"+sym)
	if f.keyHook != nil {
		f.keyHook(displayID)
	}
	return f.inputErr
}
func (f *fakeExecutor) Type(_ context.Context, displayID, message string) error {
	f.append(displayID, "type") // Never store the payload in the fake log.
	return f.inputErr
}
func (f *fakeExecutor) Click(_ context.Context, displayID string, x, y, button int) error {
	f.mu.Lock()
	f.lastClick = [3]int{x, y, button}
	f.events[displayID] = append(f.events[displayID], "click")
	f.mu.Unlock()
	return f.inputErr
}
func (f *fakeExecutor) eventsFor(display string) []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.events[display]...)
}
func (f *fakeExecutor) clickFor() [3]int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lastClick
}

func newInjector(t *testing.T, exec *fakeExecutor, tiling *fakeTiling, cap int) *inject.Injector {
	t.Helper()
	var ticks atomic.Uint64
	inj, err := inject.New(exec, tiling, func() uint64 { return ticks.Add(11) }, cap)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return inj
}
func placements() *fakeTiling {
	return &fakeTiling{placements: map[string]inject.Placement{
		"s1": {DisplayID: "d1", WindowID: "w1", X: 100, Y: 200, Width: 80, Height: 60},
		"s2": {DisplayID: "d1", WindowID: "w2", X: 190, Y: 200, Width: 80, Height: 60},
		"s3": {DisplayID: "d2", WindowID: "w3", X: 25, Y: 35, Width: 80, Height: 60},
	}}
}
func key(surface, keysym string) inject.Request {
	return inject.Request{SurfaceID: surface, Kind: inject.KindKey, Keysym: keysym}
}

// Several concurrent submitters to the same display may not interleave their
// Activate -> Focused -> Key sequences. The fake and clock are race-safe.
func TestBidInjectConcurrentSameDisplayOrdering(t *testing.T) {
	exec := newExecutor()
	exec.keyHook = func(string) { runtime.Gosched() }
	inj := newInjector(t, exec, placements(), 80)
	const n = 64
	start := make(chan struct{})
	results := make(chan error, n)
	var wg sync.WaitGroup
	for j := 0; j < n; j++ {
		wg.Add(1)
		go func(j int) {
			defer wg.Done()
			<-start
			surface := "s1"
			if j%2 == 1 {
				surface = "s2"
			}
			action, err := inj.Submit(context.Background(), key(surface, "Ctrl+L"))
			if err == nil && (!action.FocusVerified || action.InputMonoNS == 0 || action.Result != "ok") {
				err = errors.New("invalid success record")
			}
			results <- err
		}(j)
	}
	close(start)
	wg.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Errorf("Submit: %v", err)
		}
	}
	events := exec.eventsFor("d1")
	if len(events) != n*3 {
		t.Fatalf("events = %d, want %d", len(events), n*3)
	}
	for j := 0; j < len(events); j += 3 {
		if (events[j] != "activate:w1" && events[j] != "activate:w2") ||
			events[j+1] != "focus" || events[j+2] != "key:Ctrl+L" {
			t.Fatalf("interleaved events at %d: %v", j, events[j:j+3])
		}
	}
	if got := len(inj.Records()); got != n {
		t.Fatalf("records = %d, want %d", got, n)
	}
}

func TestBidInjectDifferentDisplaysParallel(t *testing.T) {
	exec := newExecutor()
	entered := make(chan struct{})
	release := make(chan struct{})
	exec.keyHook = func(display string) {
		if display == "d1" {
			close(entered)
			<-release
		}
	}
	inj := newInjector(t, exec, placements(), 2)
	first := make(chan error, 1)
	go func() { _, err := inj.Submit(context.Background(), key("s1", "A")); first <- err }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("first display did not reach executor")
	}
	other := make(chan error, 1)
	go func() { _, err := inj.Submit(context.Background(), key("s3", "B")); other <- err }()
	select {
	case err := <-other:
		if err != nil {
			t.Fatalf("other display failed while d1 blocked: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("different display was incorrectly serialised")
	}
	close(release)
	if err := <-first; err != nil {
		t.Fatalf("first display: %v", err)
	}
}

func TestBidInjectFocusMismatchMustFail(t *testing.T) {
	exec := newExecutor()
	exec.forceFocus = "another-window"
	inj := newInjector(t, exec, placements(), 1)
	got, err := inj.Submit(context.Background(), key("s1", "A"))
	if !errors.Is(err, inject.ErrFocusMismatch) {
		t.Fatalf("err = %v", err)
	}
	if got.FocusVerified || got.InputMonoNS != 0 || got.Result != "focus_mismatch" {
		t.Fatalf("mismatch receipt = %+v", got)
	}
	if events := exec.eventsFor("d1"); !reflect.DeepEqual(events, []string{"activate:w1", "focus"}) {
		t.Fatalf("mismatch must not send input: %v", events)
	}
}

func TestBidInjectKeysymAndTextMustFailInvalid(t *testing.T) {
	exec := newExecutor()
	inj := newInjector(t, exec, placements(), 2)
	for _, sym := range []string{"", "A;whoami", "$(noop)", "A B", "A\nB", "quote'", "é", strings.Repeat("A", 33)} {
		action, err := inj.Submit(context.Background(), key("s1", sym))
		if !errors.Is(err, inject.ErrInvalidRequest) || action.InputMonoNS != 0 {
			t.Errorf("expected keysym refusal (%q): %+v / %v", sym, action, err)
		}
	}
	for _, txt := range []string{"", "line\nfeed", "tab\t", "zero\x00", strings.Repeat("x", 2001), string([]byte{0xff})} {
		action, err := inj.Submit(context.Background(), inject.Request{SurfaceID: "s1", Kind: inject.KindText, Text: txt})
		if !errors.Is(err, inject.ErrInvalidRequest) || action.FocusVerified {
			t.Errorf("expected text refusal: %+v / %v", action, err)
		}
	}
	if events := exec.eventsFor("d1"); len(events) != 0 {
		t.Fatalf("invalid inputs reached executor: %v", events)
	}
	for _, r := range []inject.Request{
		key("s1", strings.Repeat("a", 32)),
		{SurfaceID: "s1", Kind: inject.KindText, Text: strings.Repeat("é", 2000)},
	} {
		action, err := inj.Submit(context.Background(), r)
		if err != nil || !action.FocusVerified || action.Result != "ok" {
			t.Fatalf("valid boundary must succeed: %+v / %v", action, err)
		}
	}
	for _, record := range inj.Records() {
		if record.SurfaceID != "s1" || record.Kind == "" {
			t.Fatalf("invalid record: %+v", record)
		}
	}
}

func TestBidInjectClickTilingMustFailOutside(t *testing.T) {
	exec := newExecutor()
	inj := newInjector(t, exec, placements(), 2)
	for _, click := range []inject.Request{
		{SurfaceID: "s1", Kind: inject.KindClick, X: -1, Y: 0, Button: 1},
		{SurfaceID: "s1", Kind: inject.KindClick, X: 0, Y: -1, Button: 1},
		{SurfaceID: "s1", Kind: inject.KindClick, X: 80, Y: 0, Button: 1},
		{SurfaceID: "s1", Kind: inject.KindClick, X: 0, Y: 60, Button: 1},
		{SurfaceID: "s1", Kind: inject.KindClick, X: 0, Y: 0, Button: 0},
		{SurfaceID: "s1", Kind: inject.KindClick, X: 0, Y: 0, Button: 4},
	} {
		action, err := inj.Submit(context.Background(), click)
		if !errors.Is(err, inject.ErrInvalidRequest) || action.FocusVerified || action.InputMonoNS != 0 {
			t.Errorf("out-of-window click not refused: %+v / %v", action, err)
		}
	}
	if got := exec.eventsFor("d1"); len(got) != 0 {
		t.Fatalf("refused clicks reached executor: %v", got)
	}
	action, err := inj.Submit(context.Background(), inject.Request{SurfaceID: "s1", Kind: inject.KindClick, X: 79, Y: 59, Button: 3})
	if err != nil || action.Result != "ok" || exec.clickFor() != [3]int{179, 259, 3} {
		t.Fatalf("valid local click: %+v / %v / %v", action, err, exec.clickFor())
	}
}

func TestBidInjectBoundedQueueMustFailFull(t *testing.T) {
	exec := newExecutor()
	entered := make(chan struct{})
	release := make(chan struct{})
	exec.keyHook = func(string) { close(entered); <-release }
	inj := newInjector(t, exec, placements(), 1) // one outstanding; zero waiting
	first := make(chan error, 1)
	go func() { _, err := inj.Submit(context.Background(), key("s1", "A")); first <- err }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("blocked executor not reached")
	}
	action, err := inj.Submit(context.Background(), key("s2", "B"))
	if !errors.Is(err, inject.ErrQueueFull) || action.Result != "queue_full" || action.FocusVerified || action.InputMonoNS != 0 {
		t.Fatalf("queue full MUST-FAIL: %+v / %v", action, err)
	}
	if got := exec.eventsFor("d1"); !reflect.DeepEqual(got, []string{"activate:w1", "focus", "key:A"}) {
		t.Fatalf("queued request activated: %v", got)
	}
	close(release)
	if err := <-first; err != nil {
		t.Fatalf("initial request failed: %v", err)
	}
	if got := len(inj.Records()); got != 2 {
		t.Fatalf("refusal not recorded; got %d", got)
	}
}

func TestBidInjectPlacementChangeMustFail(t *testing.T) {
	maps := placements()
	maps.changeAt = 2
	maps.changed = inject.Placement{DisplayID: "d2", WindowID: "w3", X: 5, Y: 5, Width: 30, Height: 30}
	exec := newExecutor()
	inj := newInjector(t, exec, maps, 1)
	action, err := inj.Submit(context.Background(), key("s1", "K"))
	if !errors.Is(err, inject.ErrPlacementChanged) || action.Result != "placement_changed" {
		t.Fatalf("stale tiling MUST-FAIL: %+v / %v", action, err)
	}
	if got := exec.eventsFor("d1"); len(got) != 0 {
		t.Fatalf("stale placement reached executor: %v", got)
	}
}

func TestBidInjectCancelledMustFail(t *testing.T) {
	exec := newExecutor()
	inj := newInjector(t, exec, placements(), 1)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	action, err := inj.Submit(ctx, key("s1", "B"))
	if !errors.Is(err, context.Canceled) || action.Result != "canceled" || action.InputMonoNS != 0 {
		t.Fatalf("cancellation MUST-FAIL: %+v / %v", action, err)
	}
	if got := exec.eventsFor("d1"); len(got) != 0 {
		t.Fatalf("cancelled request activated: %v", got)
	}
}

func TestBidInjectExecutorErrorMustFail(t *testing.T) {
	exec := newExecutor()
	exec.inputErr = errors.New("some external executor details must remain opaque")
	inj := newInjector(t, exec, placements(), 1)
	action, err := inj.Submit(context.Background(), key("s1", "K"))
	if !errors.Is(err, inject.ErrExecutor) || action.Result != "executor_error" || !action.FocusVerified || action.InputMonoNS == 0 {
		t.Fatalf("executor failure receipt: %+v / %v", action, err)
	}
	if strings.Contains(err.Error(), "external") {
		t.Fatalf("executor detail leaked: %v", err)
	}
}
