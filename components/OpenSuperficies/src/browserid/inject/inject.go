// Package inject serialises validated computer-use input per virtual display.
// It does not start processes, mount browser profiles, or handle credentials.
package inject

import (
	"context"
	"errors"
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"
)

// Kind identifies the only supported XTEST-style input operations.
type Kind string

const (
	KindKey   Kind = "key"
	KindText  Kind = "text"
	KindClick Kind = "click"
	// KindScroll sends Count wheel steps (Button 4 up, 5 down, 6 left, 7 right) at X,Y.
	KindScroll Kind = "scroll"
	// KindDrag presses Button at X,Y, moves to X2,Y2 in small steps and releases.
	KindDrag Kind = "drag"
)

// Request targets a surface. Exactly the fields for Kind may be populated.
// Click coordinates are local to the window, not to the whole display.
type Request struct {
	SurfaceID string
	Kind      Kind
	Keysym    string
	Text      string
	X, Y      int
	Button    int
	// Scroll and drag only.
	X2, Y2   int
	Count    int
	Modifier string // "", "ctrl" or "shift": held during a scroll
}

// Placement is a snapshot from a tiling map. WindowID identifies the actual
// window whose focus the Executor must verify. X/Y are display-local origins.
// The tiling implementation must guarantee non-overlap and valid display bounds.
type Placement struct {
	DisplayID string
	WindowID  string
	X, Y      int
	Width     int
	Height    int
}

// Tiling is a local, fakeable port until browserid/tiling is available.
// Implementations must be safe for concurrent Resolve calls.
type Tiling interface {
	Resolve(surfaceID string) (Placement, error)
}

// Executor is a typed, shell-free input port. Click receives display-local
// coordinates; activation and focus verification address the window by ID.
// Implementations must not read or expose browser profile files or credentials.
type Executor interface {
	Activate(ctx context.Context, displayID, windowID string) error
	Focused(ctx context.Context, displayID string) (windowID string, err error)
	Key(ctx context.Context, displayID, keysym string) error
	Type(ctx context.Context, displayID, text string) error
	Click(ctx context.Context, displayID string, x, y, button int) error
}

// PointerExecutor is the optional wheel and drag port; KindScroll and KindDrag are refused as
// executor errors when the Executor does not implement it.
type PointerExecutor interface {
	Scroll(ctx context.Context, displayID string, x, y, button, count int, modifier string) error
	Drag(ctx context.Context, displayID string, x1, y1, x2, y2, button int) error
}

// Clock returns monotonically increasing nanoseconds in the clock domain of
// the actual input executor. The caller injects it; zero is not a failure code.
type Clock func() uint64

// Action is a metadata-only receipt. Neither key nor text payload is recorded.
// A zero input_mono_ns means no input was attempted by this injector.
type Action struct {
	SurfaceID     string `json:"surface_id"`
	Kind          Kind   `json:"kind"`
	InputMonoNS   uint64 `json:"input_mono_ns"`
	FocusVerified bool   `json:"focus_verified"`
	Result        string `json:"result"`
}

var (
	ErrInvalidRequest    = errors.New("inject: invalid input")
	ErrTargetUnavailable = errors.New("inject: target unavailable")
	ErrQueueFull         = errors.New("inject: display queue full")
	ErrFocusMismatch     = errors.New("inject: focus mismatch")
	ErrExecutor          = errors.New("inject: executor failure")
	ErrPlacementChanged  = errors.New("inject: placement changed")
)

type lane struct {
	// outstanding contains all admitted requests, including the executing one.
	outstanding chan struct{}
	// turn is a binary semaphore across activate, focus check and input.
	turn chan struct{}
}

// Injector owns per-display serialisation lanes and in-memory metadata receipts.
// Use the same Injector instance for every surface sharing a display.
type Injector struct {
	executor       Executor
	tiling         Tiling
	clock          Clock
	maxOutstanding int
	lanesMu        sync.Mutex
	lanes          map[string]*lane
	recordsMu      sync.Mutex
	records        []Action
}

// New constructs a single multi-display injector. maxOutstanding limits the
// number of active-plus-waiting requests for each display, and must be >= 1.
func New(executor Executor, tiling Tiling, clock Clock, maxOutstanding int) (*Injector, error) {
	if executor == nil || tiling == nil || clock == nil || maxOutstanding < 1 {
		return nil, ErrInvalidRequest
	}
	return &Injector{
		executor: executor, tiling: tiling, clock: clock,
		maxOutstanding: maxOutstanding, lanes: make(map[string]*lane),
	}, nil
}

// Records returns a snapshot of all completed attempts, including refusals.
// It never contains the key, typed text, or a browser profile path.
func (i *Injector) Records() []Action {
	i.recordsMu.Lock()
	defer i.recordsMu.Unlock()
	return append([]Action(nil), i.records...)
}

func (i *Injector) record(action Action, err error) (Action, error) {
	i.recordsMu.Lock()
	i.records = append(i.records, action)
	i.recordsMu.Unlock()
	return action, err
}

func (i *Injector) displayLane(displayID string) *lane {
	i.lanesMu.Lock()
	defer i.lanesMu.Unlock()
	if l, ok := i.lanes[displayID]; ok {
		return l
	}
	l := &lane{
		outstanding: make(chan struct{}, i.maxOutstanding),
		turn:        make(chan struct{}, 1),
	}
	l.turn <- struct{}{}
	i.lanes[displayID] = l
	return l
}

// Submit validates the input and placement, admits it to the bounded display
// lane, then locks that display through activation, focus verification, and
// input. No Key/Type/Click is sent unless focus matches the target window.
// Every result (including a refusal) is returned and recorded in memory.
func (i *Injector) Submit(ctx context.Context, req Request) (Action, error) {
	action := Action{SurfaceID: req.SurfaceID, Kind: req.Kind}
	if ctx == nil || !validRequest(req) {
		action.Result = "invalid_input"
		return i.record(action, ErrInvalidRequest)
	}
	if err := ctx.Err(); err != nil {
		action.Result = "canceled"
		return i.record(action, err)
	}
	placement, err := i.tiling.Resolve(req.SurfaceID)
	if err != nil || !validPlacement(placement) {
		action.Result = "target_unavailable"
		return i.record(action, ErrTargetUnavailable)
	}
	if (req.Kind == KindClick || req.Kind == KindScroll || req.Kind == KindDrag) && !insideWindow(placement, req.X, req.Y) ||
		req.Kind == KindDrag && !insideWindow(placement, req.X2, req.Y2) {
		action.Result = "invalid_input"
		return i.record(action, ErrInvalidRequest)
	}

	l := i.displayLane(placement.DisplayID)
	select {
	case l.outstanding <- struct{}{}:
	default:
		action.Result = "queue_full"
		return i.record(action, ErrQueueFull)
	}
	defer func() { <-l.outstanding }()

	select {
	case <-ctx.Done():
		action.Result = "canceled"
		return i.record(action, ctx.Err())
	case <-l.turn:
	}
	defer func() { l.turn <- struct{}{} }()
	if err := ctx.Err(); err != nil {
		action.Result = "canceled"
		return i.record(action, err)
	}

	// A window moved to another display while waiting must never be injected
	// using the stale lane or the stale display-relative click coordinates.
	latest, err := i.tiling.Resolve(req.SurfaceID)
	if err != nil || !validPlacement(latest) || latest != placement {
		action.Result = "placement_changed"
		return i.record(action, ErrPlacementChanged)
	}
	if err := i.executor.Activate(ctx, placement.DisplayID, placement.WindowID); err != nil {
		action.Result = "executor_error"
		return i.record(action, ErrExecutor)
	}
	focused, err := i.executor.Focused(ctx, placement.DisplayID)
	if err != nil {
		action.Result = "focus_check_error"
		return i.record(action, ErrExecutor)
	}
	if focused != placement.WindowID {
		action.Result = "focus_mismatch"
		return i.record(action, ErrFocusMismatch)
	}
	action.FocusVerified = true
	if err := ctx.Err(); err != nil {
		action.Result = "canceled"
		return i.record(action, err)
	}

	// Read the caller-injected monotonic clock at the injection boundary.
	action.InputMonoNS = i.clock()
	switch req.Kind {
	case KindKey:
		err = i.executor.Key(ctx, placement.DisplayID, req.Keysym)
	case KindText:
		err = i.executor.Type(ctx, placement.DisplayID, req.Text)
	case KindClick:
		err = i.executor.Click(ctx, placement.DisplayID,
			placement.X+req.X, placement.Y+req.Y, req.Button)
	case KindScroll, KindDrag:
		pe, ok := i.executor.(PointerExecutor)
		switch {
		case !ok:
			err = ErrExecutor
		case req.Kind == KindScroll:
			err = pe.Scroll(ctx, placement.DisplayID, placement.X+req.X, placement.Y+req.Y, req.Button, req.Count, req.Modifier)
		default:
			err = pe.Drag(ctx, placement.DisplayID, placement.X+req.X, placement.Y+req.Y, placement.X+req.X2, placement.Y+req.Y2, req.Button)
		}
	}
	if err != nil {
		action.Result = "executor_error"
		return i.record(action, ErrExecutor)
	}
	action.Result = "ok"
	return i.record(action, nil)
}

func validRequest(req Request) bool {
	if req.SurfaceID == "" || strings.TrimSpace(req.SurfaceID) != req.SurfaceID {
		return false
	}
	switch req.Kind {
	case KindKey:
		if len(req.Keysym) < 1 || len(req.Keysym) > 32 || req.Text != "" || req.X != 0 || req.Y != 0 || req.Button != 0 {
			return false
		}
		for _, c := range req.Keysym {
			if !((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
				(c >= '0' && c <= '9') || c == '_' || c == '+') {
				return false
			}
		}
		return true
	case KindText:
		if req.Keysym != "" || req.X != 0 || req.Y != 0 || req.Button != 0 ||
			!utf8.ValidString(req.Text) || req.Text == "" || utf8.RuneCountInString(req.Text) > 2000 {
			return false
		}
		for _, c := range req.Text {
			if !unicode.IsPrint(c) {
				return false
			}
		}
		return true
	case KindClick:
		return req.Keysym == "" && req.Text == "" && req.Button >= 1 && req.Button <= 3 &&
			req.X2 == 0 && req.Y2 == 0 && req.Count == 0 && req.Modifier == ""
	case KindScroll:
		return req.Keysym == "" && req.Text == "" && req.Button >= 4 && req.Button <= 7 &&
			req.Count >= 1 && req.Count <= 20 && req.X2 == 0 && req.Y2 == 0 &&
			(req.Modifier == "" || req.Modifier == "ctrl" || req.Modifier == "shift")
	case KindDrag:
		return req.Keysym == "" && req.Text == "" && req.Button >= 1 && req.Button <= 3 &&
			req.Count == 0 && req.Modifier == "" && (req.X != req.X2 || req.Y != req.Y2)
	default:
		return false
	}
}

func validPlacement(p Placement) bool {
	if p.DisplayID == "" || p.WindowID == "" || p.X < 0 || p.Y < 0 || p.Width <= 0 || p.Height <= 0 {
		return false
	}
	maxInt := int(^uint(0) >> 1)
	return p.X <= maxInt-(p.Width-1) && p.Y <= maxInt-(p.Height-1)
}

func insideWindow(p Placement, x, y int) bool {
	return x >= 0 && y >= 0 && x < p.Width && y < p.Height
}
