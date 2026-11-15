package capture

import (
	"os"
	"path/filepath"
	"testing"

	"opensuperficies.local/srf/src/cubook/rc"
)

type memAt []byte

func (b memAt) ReadAt(p []byte, off int64) (int, error) {
	n := copy(p, b[off:])
	return n, nil
}

type noFz struct{}

func (noFz) Freeze() error { return nil }
func (noFz) Thaw() error   { return nil }

// The observer returns hashes and geometry only and writes nothing but frames.raw
// under the ring directory; a directory that is not a managed ramws run is refused
// before the X server is dialled.
func TestSnapperWritesOnlyUnderRamws(t *testing.T) {
	dir, other := t.TempDir(), t.TempDir()
	src := SynthXWD(8, 4, 0, nil)
	h, err := ParseXWDHeader(memAt(src))
	if err != nil {
		t.Fatal(err)
	}
	ring := NewRing(dir, h.Width, h.Height)
	defer ring.Close()
	o := &SnapObserver{S: &Snapper{Src: memAt(src), Freezer: noFz{}, Ring: ring, Watchdog: DefaultWatchdog}}
	f1, err := o.Observe()
	if err != nil {
		t.Fatal(err)
	}
	f2, _ := o.Observe()
	if f1.W != 8 || f1.H != 4 || len(f1.Digest) != 64 || len(f1.ScreenID) != 16 || f2.RI != f1.RI+1 {
		t.Fatalf("frames %+v %+v", f1, f2)
	}
	if f1.Digest != f2.Digest || f1.ScreenID != ScreenID(8, 4) {
		t.Fatalf("static screen must give equal digests: %+v %+v", f1, f2)
	}
	ents, _ := os.ReadDir(dir)
	if len(ents) != 1 || ents[0].Name() != "frames.raw" {
		t.Fatalf("ring dir content %v", ents)
	}
	if e, _ := os.ReadDir(other); len(e) != 0 {
		t.Fatalf("stray files %v", e)
	}
	// real constructor: outside the ramws root -> rc 5, nothing created, no X dial.
	_, err = NewSnapObserver(filepath.Join(other, "fb"), other, ":100")
	if err == nil {
		t.Fatal("non-ramws run dir accepted")
	}
	if r := rc.Code(err.(*Error).Class); r != 5 {
		t.Fatalf("rc %d", r)
	}
	if e, _ := os.ReadDir(other); len(e) != 0 {
		t.Fatalf("stray files %v", e)
	}
}

// ADW1-04: closing the observer flushes raw.json + frames.linear.raw beside the ring, so GuiAdapter can read it.
func TestSnapObserverCloseWritesRawJSON(t *testing.T) {
	dir := t.TempDir()
	src := SynthXWD(8, 4, 0, nil)
	h, _ := ParseXWDHeader(memAt(src))
	ring := NewRing(dir, h.Width, h.Height)
	o := &SnapObserver{S: &Snapper{Src: memAt(src), Freezer: noFz{}, Ring: ring, Watchdog: DefaultWatchdog}, Done: func() { ring.Close() }}
	for i := 0; i < 2; i++ {
		if _, err := o.Observe(); err != nil {
			t.Fatal(err)
		}
	}
	o.Close()
	if _, err := os.Stat(filepath.Join(dir, "raw.json")); err != nil {
		t.Fatalf("no raw.json: %v", err)
	}
	if st, err := os.Stat(filepath.Join(dir, LinearFile)); err != nil || st.Size() != int64(2*8*4*3) {
		t.Fatalf("linear %v %v", st, err)
	}
	// no frame captured -> nothing written, no error surfaced
	d2 := t.TempDir()
	o2 := &SnapObserver{S: &Snapper{Ring: NewRing(d2, 8, 4)}}
	o2.Close()
	if e, _ := os.ReadDir(d2); len(e) != 0 {
		t.Fatalf("empty observer wrote %v", e)
	}
}
