package capture

import (
	"io"
	"sync"
	"time"
)

// Freezer stops and resumes the X server around a read (real: XGrabServer+XSync / XUngrabServer).
type Freezer interface {
	Freeze() error
	Thaw() error
}

// Snapper does freeze -> read -> thaw into a Ring.
type Snapper struct {
	Src      io.ReaderAt // Xvfb_screen0 (mmap) or synthetic XWD
	Freezer  Freezer
	Ring     *Ring
	Watchdog time.Duration // freeze budget, 50 ms
	Now      func() time.Time

	start time.Time
	buf   []byte
}

// DefaultWatchdog is the hardware freeze watchdog of DESIGN_CU section 5.
const DefaultWatchdog = 50 * time.Millisecond

func (s *Snapper) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// Snap captures one frame. The server is always thawed exactly once; a freeze longer than the
// watchdog thaws early and discards the (possibly torn) frame with a gate stop.
func (s *Snapper) Snap() (FrameMeta, error) {
	wd := s.Watchdog
	if wd <= 0 {
		wd = DefaultWatchdog
	}
	if err := s.Freezer.Freeze(); err != nil {
		return FrameMeta{}, ioErr("freeze_failed", err)
	}
	var once sync.Once
	var thawErr error
	thaw := func() { once.Do(func() { thawErr = s.Freezer.Thaw() }) }
	fired := false
	var mu sync.Mutex
	timer := time.AfterFunc(wd, func() {
		mu.Lock()
		fired = true
		mu.Unlock()
		thaw()
	})
	t0 := s.now()
	hdr, rgb, rerr := s.read()
	timer.Stop()
	thaw()
	mu.Lock()
	tripped := fired
	mu.Unlock()
	if tripped || s.now().Sub(t0) > wd {
		return FrameMeta{}, gate("freeze_watchdog")
	}
	if thawErr != nil {
		return FrameMeta{}, ioErr("thaw_failed", thawErr)
	}
	if rerr != nil {
		return FrameMeta{}, rerr
	}
	_ = hdr
	if s.start.IsZero() {
		s.start = t0
	}
	return s.Ring.Put(rgb, uint64(t0.Sub(s.start).Microseconds()))
}

func (s *Snapper) read() (Header, []byte, error) {
	h, err := ParseXWDHeader(s.Src)
	if err != nil {
		return h, nil, err
	}
	if h.Width != s.Ring.W || h.Height != s.Ring.H {
		return h, nil, &Error{Class: ClassStale, Reason: "geometry_changed"}
	}
	need := h.BytesPerLine * h.Height
	if cap(s.buf) < need {
		s.buf = make([]byte, need)
	}
	raw := s.buf[:need]
	if n, err := s.Src.ReadAt(raw, int64(h.PixelOffset)); n < need {
		return h, nil, ioErr("pixels_short", err)
	}
	rgb := make([]byte, h.Width*h.Height*3)
	BGRXToRGB24(rgb, raw, h.Width, h.Height, h.BytesPerLine)
	return h, rgb, nil
}
