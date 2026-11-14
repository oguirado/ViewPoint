package capture

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

const tw, th = 64, 48

type fakeSrc struct {
	clk   *int64
	cost  func(n int) int64
	n     int
	run   string
	maxCh int
}

func (f *fakeSrc) Frame(dst []byte) error {
	if f.run != "" {
		ents, _ := os.ReadDir(f.run)
		c := 0
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), "chunk_") {
				c++
			}
		}
		if c > f.maxCh {
			f.maxCh = c
		}
	}
	for i := range dst {
		dst[i] = byte(f.n*31 + i)
	}
	if f.cost != nil {
		*f.clk += f.cost(f.n)
	}
	f.n++
	return nil
}

type rig struct {
	cfg Config
	src *fakeSrc
	clk *int64
	run string
	out string
}

func newRig(t *testing.T) *rig {
	t.Helper()
	root := t.TempDir()
	run := filepath.Join(root, "r1")
	os.Mkdir(run, 0o700)
	os.WriteFile(filepath.Join(run, "owner.json"), []byte("{}"), 0o600)
	lk, err := os.OpenFile(filepath.Join(run, ".lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	syscall.Flock(int(lk.Fd()), syscall.LOCK_EX)
	t.Cleanup(func() { lk.Close() })
	clk := new(int64)
	*clk = 1_000_000_000
	src := &fakeSrc{clk: clk, run: run}
	out := t.TempDir()
	cfg := Config{
		Guard: PathGuard{Root: root, FSType: fsOf("tmpfs")}, RunDir: run, OutDir: out,
		W: tw, H: th, Frames: 20, ChunkFrames: 4, Mode: ModeLive, QuotaBytes: DefaultQuota,
		Clock: func() int64 { return *clk }, Sleep: func(ns int64) { *clk += ns },
		NowSec: func() int64 { return 1000 }, Source: src, OutFS: fsOf("ext4"),
	}
	return &rig{cfg: cfg, src: src, clk: clk, run: run, out: out}
}

func reason(err error) (string, string) {
	var ce *Error
	if errors.As(err, &ce) {
		return ce.Class, ce.Reason
	}
	return "", ""
}

func TestPacerIntegerNs60fps(t *testing.T) {
	p := NewPacer(FrameNs)
	for i := int64(0); i < 600; i++ {
		idx, miss, ok := p.Tick(5 + i*FrameNs)
		if !ok || idx != uint64(i) || miss != 0 {
			t.Fatalf("tick %d: idx=%d miss=%d ok=%v", i, idx, miss, ok)
		}
		if w := p.WaitNs(); w <= 0 || w > FrameNs {
			t.Fatalf("wait %d out of (0,Δ]", w)
		}
	}
	if FrameNs != 16_666_667 {
		t.Fatal("Δ not 16,666,667 ns")
	}
}

func TestPacerOverrunMakesRenderIndexGap(t *testing.T) {
	p := NewPacer(FrameNs)
	p.Tick(0)
	if idx, _, ok := p.Tick(FrameNs); !ok || idx != 1 {
		t.Fatal("tick 1")
	}
	idx, miss, ok := p.Tick(4*FrameNs + 5)
	if !ok || idx != 4 || miss != 2 {
		t.Fatalf("overrun: idx=%d miss=%d ok=%v (want 4,2,true)", idx, miss, ok)
	}
}

func TestPacerNegativeDeltaClampedZero(t *testing.T) {
	p := NewPacer(FrameNs)
	p.Tick(10 * FrameNs)
	p.Tick(11 * FrameNs)
	if _, _, ok := p.Tick(11*FrameNs - 1000); ok {
		t.Fatal("backwards clock produced a frame")
	}
	if idx, miss, ok := p.Tick(12 * FrameNs); !ok || idx != 2 || miss != 0 {
		t.Fatalf("after clamp: idx=%d miss=%d ok=%v", idx, miss, ok)
	}
}

func TestChunkReleasedBeforeNextBeyondTwo(t *testing.T) {
	r := newRig(t)
	var order []string
	r.cfg.Release = func(d string) error {
		order = append(order, filepath.Base(d))
		return os.RemoveAll(d)
	}
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Chunks) != 5 || r.src.maxCh == 0 || r.src.maxCh > 2 {
		t.Fatalf("chunks=%d maxResident=%d (want 5, 1..2)", len(res.Chunks), r.src.maxCh)
	}
	if res.PeakResident > 2*int64(4*tw*th*3) || res.PeakResident == 0 {
		t.Fatalf("peak %d", res.PeakResident)
	}
	if len(order) != 5 || order[0] != "chunk_0" || order[4] != "chunk_4" {
		t.Fatalf("release order %v", order)
	}
	if ents, _ := os.ReadDir(r.run); countPrefix(ents, "chunk_") != 0 {
		t.Fatal("chunks left after run")
	}
}

func countPrefix(ents []os.DirEntry, p string) int {
	n := 0
	for _, e := range ents {
		if strings.HasPrefix(e.Name(), p) {
			n++
		}
	}
	return n
}

func TestConservationPublishedEqualsFramesPlusOverrun(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames = 60
	r.src.cost = func(n int) int64 {
		if n%7 == 6 {
			return 5 * FrameNs / 2
		}
		return 0
	}
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if res.Overrun == 0 || len(res.Gaps) == 0 {
		t.Fatal("no overrun produced")
	}
	var sum uint64
	for _, g := range res.Gaps {
		sum += g.Missing
	}
	if res.Published != res.Frames+res.Overrun+res.Dropped || sum != res.Overrun || res.Dropped != 0 {
		t.Fatalf("P=%d F=%d O=%d D=%d gaps=%d", res.Published, res.Frames, res.Overrun, res.Dropped, sum)
	}
	b, err := os.ReadFile(filepath.Join(r.out, "gaps.jsonl"))
	if err != nil || strings.Count(string(b), "\n") != len(res.Gaps) || !strings.Contains(string(b), `"missing"`) {
		t.Fatalf("gaps.jsonl %q %v", b, err)
	}
	rb, err := os.ReadFile(filepath.Join(r.out, "receipt.json"))
	if err != nil || !strings.Contains(string(rb), `"published"`) || !strings.Contains(string(rb), `"overrun"`) {
		t.Fatalf("receipt %q %v", rb, err)
	}
}

func TestMmapBoundsNoAddOverflow(t *testing.T) {
	m := ^uint64(0)
	cases := []struct {
		o, l, m uint64
		want    bool
	}{
		{0, 0, 0, true}, {m, 0, m, true}, {m, 1, m, false}, {1 << 63, 1 << 63, m, false},
		{5, 3, 8, true}, {5, 4, 8, false}, {9, 0, 8, false}, {m - 1, 2, m, false}, {m - 1, 1, m, true},
	}
	for _, c := range cases {
		if got := InBounds(c.o, c.l, c.m); got != c.want {
			t.Errorf("InBounds(%d,%d,%d)=%v want %v", c.o, c.l, c.m, got, c.want)
		}
	}
	x := SynthXWD(tw, th, 0, nil)
	s := &XWDSource{Src: bytesAt(x), Size: int64(len(x)) - 1}
	if err := s.Frame(make([]byte, tw*th*3)); err == nil {
		t.Fatal("short mapping accepted")
	}
	s = &XWDSource{Src: bytesAt(x), Size: int64(len(x))}
	dst := make([]byte, tw*th*3)
	if err := s.Frame(dst); err != nil || dst[3] != 7 {
		t.Fatalf("good mapping: %v", err)
	}
}

type bytesAt []byte

func (b bytesAt) ReadAt(p []byte, off int64) (int, error) {
	if off >= int64(len(b)) {
		return 0, errors.New("eof")
	}
	return copy(p, b[off:]), nil
}

func TestDetShaOffInLiveOnInWL(t *testing.T) {
	r := newRig(t)
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range res.Chunks {
		if len(c.Det) != 0 {
			t.Fatal("live run computed det_sha256")
		}
	}
	w := newRig(t)
	w.cfg.Mode, w.cfg.Det, w.cfg.Keep, w.cfg.Frames = ModeWL, true, true, 8
	res, err = Run(w.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Chunks) != 2 || len(res.Chunks[0].Det) != 4 {
		t.Fatalf("wl det rows: %+v", res.Chunks)
	}
	fr := make([]byte, tw*th*3)
	for i := range fr {
		fr[i] = byte(i)
	}
	sum := sha256.Sum256(fr)
	if res.Chunks[0].Det[0] != hex.EncodeToString(sum[:]) {
		t.Fatal("det sha mismatch frame 0")
	}
	bad := newRig(t)
	bad.cfg.Det = true
	if _, err := Run(bad.cfg); err == nil {
		t.Fatal("live+det accepted")
	} else if c, rs := reason(err); c != ClassUsage || rs != "det_not_allowed_live" {
		t.Fatal(c, rs)
	}
	bad = newRig(t)
	bad.cfg.Mode = ModeWL
	if _, err := Run(bad.cfg); err == nil {
		t.Fatal("wl without det accepted")
	} else if c, rs := reason(err); c != ClassUsage || rs != "det_required_wl" {
		t.Fatal(c, rs)
	}
}

func TestSegmentShortRc5(t *testing.T) {
	r := newRig(t)
	r.cfg.Mode, r.cfg.Det, r.cfg.Keep, r.cfg.Frames, r.cfg.MinSegmentMs = ModeWL, true, true, 60, MinSegmentMs
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "segment_short" {
		t.Fatalf("want E_GATE segment_short got %v", err)
	}
	ok := newRig(t)
	ok.cfg.Mode, ok.cfg.Det, ok.cfg.Keep, ok.cfg.Frames, ok.cfg.MinSegmentMs, ok.cfg.ChunkFrames = ModeWL, true, true, 300, MinSegmentMs, 60
	res, err := Run(ok.cfg)
	if err != nil || res.SegmentMs < 5000 {
		t.Fatalf("300 clean frames: %v seg=%d", err, res.SegmentMs)
	}
}

func TestLiveDriverRefusesNonRamws(t *testing.T) {
	r := newRig(t)
	r.cfg.Guard.FSType = fsOf("ext4")
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "E_RAM_NOT_TMPFS" {
		t.Fatalf("ext4: %v", err)
	}
	r = newRig(t)
	r.cfg.RunDir = t.TempDir()
	_, err = Run(r.cfg)
	if c, _ := reason(err); c != ClassGate {
		t.Fatalf("outside root: %v", err)
	}
	r = newRig(t)
	r.cfg.OutDir = "/dev/shm/x-out"
	_, err = Run(r.cfg)
	if c, rs := reason(err); c != ClassUsage || rs != "out_in_ram" {
		t.Fatalf("out in ram: %v", err)
	}
}

func TestQuotaRefusedBeforeCapture(t *testing.T) {
	r := newRig(t)
	r.cfg.QuotaBytes = int64(2*4*tw*th*3) - 1
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "E_RAM_QUOTA" {
		t.Fatalf("want E_RAM_QUOTA got %v", err)
	}
	if r.src.n != 0 {
		t.Fatal("captured before refusing")
	}
	k := newRig(t)
	k.cfg.Mode, k.cfg.Det, k.cfg.Keep, k.cfg.Frames = ModeWL, true, true, 8
	k.cfg.QuotaBytes = int64(8*tw*th*3) - 1
	if _, err := Run(k.cfg); err == nil {
		t.Fatal("keep beyond quota accepted")
	}
}

func TestRetention4KRefusedExceptHandover1s(t *testing.T) {
	r := newRig(t)
	r.cfg.W, r.cfg.H, r.cfg.Mode, r.cfg.Det, r.cfg.Keep = 3840, 2160, ModeWL, true, true
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "retention_refused" {
		t.Fatalf("4K keep: %v", err)
	}
	h := newRig(t)
	h.cfg.W, h.cfg.H, h.cfg.Mode, h.cfg.Keep, h.cfg.Frames = 3840, 2160, ModeHandover1s, true, 59
	_, err = Run(h.cfg)
	if c, rs := reason(err); c != ClassUsage || rs != "handover_1s_frames" {
		t.Fatalf("handover wrong frames: %v", err)
	}
	h = newRig(t)
	h.cfg.W, h.cfg.H, h.cfg.Mode, h.cfg.Keep, h.cfg.Frames, h.cfg.ChunkFrames = 1920, 1080, ModeHandover1s, true, 60, 60
	_, err = Run(h.cfg)
	if c, rs := reason(err); c != ClassUsage || rs != "handover_1s_res" {
		t.Fatalf("handover non-4K: %v", err)
	}
}

func TestChunkDirUnlinkableByHost(t *testing.T) {
	r := newRig(t)
	r.cfg.Mode, r.cfg.Det, r.cfg.Keep, r.cfg.Frames = ModeWL, true, true, 8
	if _, err := Run(r.cfg); err != nil {
		t.Fatal(err)
	}
	// AW3-07: no world bits (umask 022 in the test turns 0o770/0o660 into 0o750/0o640); the owner keeps rwx so the host unlinks.
	st, err := os.Stat(filepath.Join(r.run, "chunk_0"))
	if err != nil || st.Mode().Perm()&0o007 != 0 || st.Mode().Perm()&0o700 != 0o700 {
		t.Fatalf("chunk dir %v %v", st, err)
	}
	fst, err := os.Stat(filepath.Join(r.run, "chunk_0", "frames.rgb24"))
	if err != nil || fst.Mode().Perm()&0o007 != 0 || fst.Mode().Perm()&0o600 != 0o600 || fst.Size() != int64(4*tw*th*3) {
		t.Fatalf("frames file %v %v", fst, err)
	}
	if err := os.RemoveAll(filepath.Join(r.run, "chunk_0")); err != nil { // host unlink of the released chunk
		t.Fatalf("host unlink: %v", err)
	}
}

func TestTTLExpiryReleasesRun(t *testing.T) {
	r := newRig(t)
	r.cfg.Mode, r.cfg.Det, r.cfg.Keep, r.cfg.Frames, r.cfg.TTLSeconds = ModeWL, true, true, 8, 7200
	if _, err := Run(r.cfg); err != nil {
		t.Fatal(err)
	}
	if done, err := ReapExpired(r.run, 1000+7199); err != nil || done {
		t.Fatalf("reaped early: %v %v", done, err)
	}
	done, err := ReapExpired(r.run, 1000+7200)
	if err != nil || !done {
		t.Fatalf("not reaped at ttl: %v %v", done, err)
	}
	if ents, _ := os.ReadDir(r.run); countPrefix(ents, "chunk_") != 0 {
		t.Fatal("chunks left after TTL")
	}
}

func TestKeptRunAlwaysWritesTTLMarker(t *testing.T) {
	for _, mode := range []string{ModeWL, ModeLive} {
		r := newRig(t)
		r.cfg.Mode, r.cfg.Det, r.cfg.Keep, r.cfg.Frames, r.cfg.TTLSeconds = mode, mode == ModeWL, true, 8, 0
		if _, err := Run(r.cfg); err != nil {
			t.Fatal(mode, err)
		}
		b, err := os.ReadFile(filepath.Join(r.run, "handover.json"))
		if err != nil || !strings.Contains(string(b), `"ttl_until":8200`) {
			t.Fatalf("%s: handover.json %q %v", mode, b, err)
		}
		if done, err := ReapExpired(r.run, 1000+7200); err != nil || !done {
			t.Fatalf("%s: not reaped: %v %v", mode, done, err)
		}
	}
	r := newRig(t)
	r.cfg.Mode, r.cfg.Keep, r.cfg.NowSec = ModeLive, true, nil
	if _, err := Run(r.cfg); err == nil {
		t.Fatal("kept run without a wall clock accepted")
	}
}

func TestFailedKeepRunReleasesAllChunks(t *testing.T) {
	r := newRig(t)
	r.cfg.Mode, r.cfg.Det, r.cfg.Keep, r.cfg.Frames, r.cfg.MinSegmentMs = ModeWL, true, true, 8, MinSegmentMs
	if _, err := Run(r.cfg); err == nil {
		t.Fatal("segment_short expected")
	}
	ents, _ := os.ReadDir(r.run)
	if countPrefix(ents, "chunk_") != 0 {
		t.Fatal("chunks left after failed kept run")
	}
	if _, err := os.Stat(filepath.Join(r.run, "handover.json")); err == nil {
		t.Fatal("handover.json left after failed kept run")
	}
	a := newRig(t)
	a.cfg.Keep, a.cfg.Frames = true, 8
	n := 0
	a.cfg.Source = failAfter{a.src, &n, 5}
	if _, err := Run(a.cfg); err == nil {
		t.Fatal("source failure expected")
	}
	ents, _ = os.ReadDir(a.run)
	if countPrefix(ents, "chunk_") != 0 {
		t.Fatal("chunks left after aborted kept run")
	}
}

type failAfter struct {
	s *fakeSrc
	n *int
	k int
}

func (f failAfter) Frame(dst []byte) error {
	*f.n++
	if *f.n > f.k {
		return ioErr("fake_fail", nil)
	}
	return f.s.Frame(dst)
}

func TestTailOverrunLoggedAsGap(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames = 10
	r.src.cost = func(n int) int64 {
		if n == 7 {
			return 3 * FrameNs
		}
		return 0
	}
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	var sum uint64
	for _, g := range res.Gaps {
		sum += g.Missing
	}
	if res.Published != 10 || res.Published != res.Frames+res.Overrun+res.Dropped || sum != res.Overrun || res.Overrun == 0 {
		t.Fatalf("P=%d F=%d O=%d gaps=%d", res.Published, res.Frames, res.Overrun, sum)
	}
	if cg := chunkGaps(res); cg != len(res.Gaps) { // ADC2-W2-03: the overrun after a closed chunk counts on the last chunk
		t.Fatalf("sum(chunk gaps)=%d len(gaps)=%d", cg, len(res.Gaps))
	}
	rb, _ := os.ReadFile(filepath.Join(r.out, "receipt.json"))
	if !strings.Contains(string(rb), `"requested":10`) || !strings.Contains(string(rb), `"published":10`) {
		t.Fatalf("receipt %s", rb)
	}
}

func TestValidate4KChunkAndQuotaCeilings(t *testing.T) {
	want := func(t *testing.T, c Config, rs string) {
		t.Helper()
		_, err := Run(c)
		if cl, got := reason(err); cl != ClassGate || got != rs {
			t.Fatalf("want gate %s got %v", rs, err)
		}
	}
	r := newRig(t)
	r.cfg.W, r.cfg.H, r.cfg.Mode, r.cfg.Det, r.cfg.ChunkFrames, r.cfg.Frames = 3840, 2160, ModeWL, true, 60, 300
	r.cfg.QuotaBytes = 8 << 30
	want(t, r.cfg, "chunk_4k_max_30")
	q := newRig(t)
	q.cfg.QuotaBytes = 16 << 30
	want(t, q.cfg, "E_RAM_QUOTA")
	h := newRig(t)
	h.cfg.W, h.cfg.H, h.cfg.Mode, h.cfg.Keep, h.cfg.Frames, h.cfg.ChunkFrames = 3840, 2160, ModeHandover1s, true, 60, 30
	h.cfg.QuotaBytes = 3 << 30
	want(t, h.cfg, "E_RAM_QUOTA")
}

func chunkGaps(res *Result) int {
	n := 0
	for _, c := range res.Chunks {
		n += c.Gaps
	}
	return n
}

func TestMidRunGapAfterChunkCloseCountedOnAChunk(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames = 12
	r.src.cost = func(n int) int64 { // frame 3 closes chunk 0 and then overruns 3 slots
		if n == 3 {
			return 3 * FrameNs
		}
		return 0
	}
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Gaps) != 1 || chunkGaps(res) != len(res.Gaps) {
		t.Fatalf("gaps=%d sum(chunk gaps)=%d", len(res.Gaps), chunkGaps(res))
	}
}

func TestHandover1sDefaultTTL(t *testing.T) {
	base := newRig(t).cfg
	base.W, base.H, base.Mode, base.Frames, base.ChunkFrames, base.QuotaBytes, base.TTLSeconds = 3840, 2160, ModeHandover1s, 60, 30, DefaultQuota, 0
	c := base
	if err := validate(&c); err != nil {
		t.Fatal(err)
	}
	if c.TTLSeconds != 7200 || !c.Keep {
		t.Fatalf("ttl=%d keep=%v", c.TTLSeconds, c.Keep)
	}
	n := base
	n.NowSec = nil
	if cl, rs := reason(validate(&n)); cl != ClassUsage || rs != "ttl_clock" {
		t.Fatalf("nil NowSec: %s %s", cl, rs)
	}
}

func TestFailedReleaseKeepsResident(t *testing.T) {
	r := newRig(t)
	r.cfg.Release = func(d string) error {
		if filepath.Base(d) == "chunk_0" {
			return errors.New("busy")
		}
		return os.RemoveAll(d)
	}
	res, err := Run(r.cfg)
	if cl, rs := reason(err); cl != ClassIO || rs != "chunk_release" {
		t.Fatalf("want io chunk_release got %v", err)
	}
	if want := int64(4 * tw * th * 3); res.ResidentEnd != want { // chunk_0 stays accounted, chunk_1 was released
		t.Fatalf("ResidentEnd=%d want %d", res.ResidentEnd, want)
	}
}

func TestTTLCapAndOverflowSafeDeadline(t *testing.T) {
	for _, ttl := range []int64{7201, 1 << 62, 9223372036854775807} {
		r := newRig(t)
		r.cfg.Keep, r.cfg.TTLSeconds = true, ttl
		if cl, rs := reason(func() error { _, e := Run(r.cfg); return e }()); cl != ClassGate || rs != "ttl_exceeded" {
			t.Fatalf("ttl %d: %s %s", ttl, cl, rs)
		}
	}
	r := newRig(t)
	r.cfg.Keep, r.cfg.TTLSeconds, r.cfg.Frames = true, 7200, 8
	r.cfg.NowSec = func() int64 { return 9223372036854775807 - 10 } // now + ttl would overflow to a negative ttl_until
	if _, err := Run(r.cfg); err == nil {
		t.Fatal("overflowing ttl_until accepted")
	}
	ents, _ := os.ReadDir(r.run)
	if countPrefix(ents, "chunk_") != 0 {
		t.Fatal("chunks left after refused deadline")
	}
	ok := newRig(t)
	ok.cfg.Keep, ok.cfg.TTLSeconds, ok.cfg.Frames = true, 7200, 8
	if _, err := Run(ok.cfg); err != nil {
		t.Fatalf("ttl at the cap refused: %v", err)
	}
}

func TestReapExpiredSurfacesReadErrors(t *testing.T) {
	d := t.TempDir()
	if done, err := ReapExpired(d, 1); err != nil || done { // no marker: nothing to do
		t.Fatalf("absent marker: %v %v", done, err)
	}
	os.Mkdir(filepath.Join(d, "handover.json"), 0o700) // ReadFile on a directory: EISDIR, not ENOENT
	if _, err := ReapExpired(d, 1); err == nil {
		t.Fatal("non-ENOENT read error swallowed")
	} else if cl, rs := reason(err); cl != ClassIO || rs != "handover_read" {
		t.Fatalf("want io handover_read got %v", err)
	}
}

// W3 INT (os.p.hookrel.P6): the worker itself releases a chunk whose hook failed, before the next chunk is analysed; the
// abort path's releaseAll must not be the only thing that frees it (AW3-02).
func TestFailedHookChunkReleasedByWorkerBeforeNext(t *testing.T) {
	r := newRig(t)
	r.cfg.Release = func(d string) error { return os.RemoveAll(d) }
	var seenSecond, firstGone bool
	r.cfg.Hook = func(dir string, row *ChunkRow) error {
		switch filepath.Base(dir) {
		case "chunk_0":
			return errors.New("boom")
		case "chunk_1":
			seenSecond = true
			_, err := os.Stat(filepath.Join(filepath.Dir(dir), "chunk_0"))
			firstGone = os.IsNotExist(err)
		}
		return nil
	}
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "analyze_failed" {
		t.Fatalf("err=%v", err)
	}
	if !seenSecond || !firstGone {
		t.Fatalf("second chunk analysed=%v, failed chunk already released=%v", seenSecond, firstGone)
	}
}
