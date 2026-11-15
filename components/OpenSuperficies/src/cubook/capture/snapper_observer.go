package capture

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"

	"opensuperficies.local/srf/src/cubook/exec"
)

// SnapObserver is the production exec.Observer: every Observe is one
// freeze -> read -> thaw Snap into the ramws ring. Pixels stay in the ring file;
// only hashes and geometry are returned.
type SnapObserver struct {
	S    *Snapper
	Done func() // closes the ring / X connection / mmap
}

// ScreenID is the 16-hex layout identity of a geometry (no pixel content).
func ScreenID(w, h int) string {
	var b [8]byte
	binary.BigEndian.PutUint32(b[0:], uint32(w))
	binary.BigEndian.PutUint32(b[4:], uint32(h))
	s := sha256.Sum256(b[:])
	return hex.EncodeToString(s[:8])
}

// Observe implements exec.Observer.
func (o *SnapObserver) Observe() (exec.Frame, error) {
	m, err := o.S.Snap()
	if err != nil {
		return exec.Frame{}, err
	}
	return exec.Frame{RI: m.RenderIndex, ScreenID: ScreenID(o.S.Ring.W, o.S.Ring.H), Digest: m.SHA256, W: o.S.Ring.W, H: o.S.Ring.H}, nil
}

// Close releases the resources handed to the observer.
func (o *SnapObserver) Close() {
	// Best effort: OS captures must leave raw.json + frames.linear.raw for the engine GuiAdapter.
	if o.S != nil && o.S.Ring != nil && o.S.Ring.Total() > 0 {
		_, _ = o.S.Ring.WriteRawJSON()
	}
	if o.Done != nil {
		o.Done()
	}
}

// NewSnapObserver opens the capsule framebuffer fb (Xvfb_screen0), dials display
// and keeps the ring under ramDir, which must be a checked ramws run directory.
func NewSnapObserver(fb, ramDir, display string) (*SnapObserver, error) {
	if err := DefaultGuard().CheckRunDir(ramDir); err != nil {
		return nil, err
	}
	xf, conn, err := DialX(display)
	if err != nil {
		return nil, err
	}
	mm, err := MmapFile(fb)
	if err != nil {
		conn.Close()
		return nil, err
	}
	h, err := ParseXWDHeader(mm)
	if err != nil {
		mm.Close()
		conn.Close()
		return nil, err
	}
	ring := NewRing(ramDir, h.Width, h.Height)
	return &SnapObserver{
		S:    &Snapper{Src: mm, Freezer: xf, Ring: ring, Watchdog: DefaultWatchdog},
		Done: func() { ring.Close(); mm.Close(); conn.Close() },
	}, nil
}
