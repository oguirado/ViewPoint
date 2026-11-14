package capture

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func num(m map[string]any, k string) int64 {
	if v, ok := m[k].(float64); ok {
		return int64(v)
	}
	return -1
}

func readReceipt(t *testing.T, out string) map[string]any {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(out, "receipt.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

// alternate overrun: every odd frame costs 2 slots so one slot is lost after it (the 4K receipt shape, 33/60).
func altOverrun(n int) int64 {
	if n%2 == 1 {
		return 2 * FrameNs
	}
	return 0
}

func TestReceiptSpanMs1sWithGaps(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames, r.cfg.ChunkFrames = 60, 30
	r.src.cost = altOverrun
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	m := readReceipt(t, r.out)
	span, run := num(m, "span_ms"), num(m, "longest_run_ms")
	if span < 1000 || span > 1017 {
		t.Fatalf("span_ms=%d want [1000,1017]", span)
	}
	if len(res.Gaps) == 0 || run >= span || num(m, "segment_ms") != run {
		t.Fatalf("gaps=%d longest_run_ms=%d segment_ms=%v span=%d", len(res.Gaps), run, m["segment_ms"], span)
	}
	if res.SpanMs != span || res.LongestRunMs != run {
		t.Fatalf("result span=%d run=%d", res.SpanMs, res.LongestRunMs)
	}
}

func TestSegmentGappyRc5WhenContinuousRequired(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames, r.cfg.ChunkFrames, r.cfg.RequireContinuous = 60, 30, true
	r.src.cost = altOverrun
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "segment_gappy" {
		t.Fatalf("gappy: %v", err)
	}
	if m := readReceipt(t, r.out); num(m, "continuous_required") != 1 {
		t.Fatalf("receipt continuous_required=%v", m["continuous_required"])
	}
	if ents, _ := os.ReadDir(r.run); len(ents) != 2 { // owner.json + .lock only: every chunk released
		t.Fatalf("run dir not emptied: %v", ents)
	}
	ok := newRig(t) // positive: a gap-free run passes the same flag
	ok.cfg.Frames, ok.cfg.ChunkFrames, ok.cfg.RequireContinuous = 60, 30, true
	if _, err := Run(ok.cfg); err != nil {
		t.Fatalf("continuous run refused: %v", err)
	}
}

func TestHandover1sGappyRecordedNotRefused(t *testing.T) {
	r := newRig(t) // RequireContinuous=false is the handover-1s default (main.go); gaps are recorded, rc 0
	r.cfg.Frames, r.cfg.ChunkFrames = 60, 30
	r.src.cost = altOverrun
	res, err := Run(r.cfg)
	if err != nil || len(res.Gaps) == 0 {
		t.Fatalf("err=%v gaps=%d", err, len(res.Gaps))
	}
	if m := readReceipt(t, r.out); num(m, "continuous_required") != 0 || num(m, "frames") >= 60 {
		t.Fatalf("receipt %v", m)
	}
}

func TestConservationWithSpan(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames, r.cfg.ChunkFrames = 61, 30
	r.src.cost = func(n int) int64 { // heavy tail: the last slots are lost and still visible
		if n >= 50 {
			return 4 * FrameNs
		}
		return altOverrun(n)
	}
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if res.Published != res.Frames+res.Overrun+res.Dropped || res.Published != 61 {
		t.Fatalf("published=%d frames=%d overrun=%d dropped=%d", res.Published, res.Frames, res.Overrun, res.Dropped)
	}
	if want := int64(res.Published) * FrameNs / 1_000_000; res.SpanMs != want {
		t.Fatalf("span_ms=%d want %d (tail gap included)", res.SpanMs, want)
	}
}

func TestChunkHookBeforeRelease(t *testing.T) {
	r := newRig(t)
	var mu sync.Mutex
	var order []string
	r.cfg.Hook = func(dir string, row *ChunkRow) error {
		_, err := os.Stat(filepath.Join(dir, "frames.rgb24"))
		mu.Lock()
		order = append(order, "hook:"+filepath.Base(dir)+map[bool]string{true: ":present", false: ":absent"}[err == nil])
		mu.Unlock()
		return nil
	}
	r.cfg.Release = func(d string) error {
		mu.Lock()
		order = append(order, "rel:"+filepath.Base(d))
		mu.Unlock()
		return os.RemoveAll(d)
	}
	if _, err := Run(r.cfg); err != nil {
		t.Fatal(err)
	}
	if len(order) < 4 || order[0] != "hook:chunk_0:present" {
		t.Fatalf("order %v", order)
	}
	seen := map[string]bool{}
	for _, o := range order {
		if len(o) > 4 && o[:4] == "rel:" && !seen[o[4:]] {
			t.Fatalf("released before analysed: %v", order)
		}
		if len(o) > 5 && o[:5] == "hook:" {
			seen[o[5:12]] = true
		}
	}
	f := newRig(t) // a failing hook still releases the chunk and ends rc 5 analyze_failed
	var rel atomic.Int32
	f.cfg.Hook = func(string, *ChunkRow) error { return errors.New("boom") }
	f.cfg.Release = func(d string) error { rel.Add(1); return os.RemoveAll(d) }
	_, err := Run(f.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "analyze_failed" || rel.Load() == 0 {
		t.Fatalf("err=%v releases=%d", err, rel.Load())
	}
}

func TestPacerNotBlockedByStuckHook(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames, r.cfg.ChunkFrames = 40, 4
	unblock := make(chan struct{})
	r.cfg.Hook = func(dir string, row *ChunkRow) error {
		if filepath.Base(dir) == "chunk_0" {
			<-unblock
		}
		return nil
	}
	var ticks atomic.Int64
	r.cfg.Clock = func() int64 { return ticks.Add(1_000_000) } // auto-advancing 1 ms clock: a stuck hook cannot freeze time
	r.cfg.Sleep = func(ns int64) { ticks.Add(ns) }
	time.AfterFunc(300*time.Millisecond, func() { close(unblock) })
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if res.Overrun == 0 || res.Frames >= 40 || r.src.maxCh > 2 || res.Published != res.Frames+res.Overrun {
		t.Fatalf("frames=%d overrun=%d maxCh=%d: pacer was blocked or a 3rd chunk opened", res.Frames, res.Overrun, r.src.maxCh)
	}
}

func TestChunkModesNoWorldBits(t *testing.T) {
	r := newRig(t)
	r.cfg.Keep, r.cfg.Frames, r.cfg.ChunkFrames = true, 8, 4
	if _, err := Run(r.cfg); err != nil {
		t.Fatal(err)
	}
	paths := []string{
		filepath.Join(r.run, "chunk_0"), filepath.Join(r.run, "chunk_0", "frames.rgb24"),
		filepath.Join(r.run, "chunk_1", "frames.rgb24"), filepath.Join(r.run, "handover.json"),
		filepath.Join(r.out, "receipt.json"), filepath.Join(r.out, "rows.jsonl"), filepath.Join(r.out, "gaps.jsonl"),
	}
	for _, p := range paths {
		st, err := os.Stat(p)
		if err != nil {
			t.Fatal(err)
		}
		if st.Mode().Perm()&0o007 != 0 {
			t.Fatalf("%s has world bits: %o", p, st.Mode().Perm())
		}
	}
	if st, _ := os.Stat(filepath.Join(r.run, "chunk_0")); st.Mode().Perm()&0o070 == 0 {
		t.Fatal("chunk dir lost its group bits (host unlink needs the ACL mask)")
	}
}

func TestNoChmodWorld(t *testing.T) {
	b, err := os.ReadFile("livedriver.go")
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`frames\.rgb24`).Match(b) {
		t.Fatal("livedriver.go not read")
	}
	if re := regexp.MustCompile(`0o(777|755|666|644)|os\.Chmod\(`); re.Match(b) {
		t.Fatalf("world-bit mode or Chmod left: %q", re.Find(b))
	}
}

func TestRequireContinuousDefault(t *testing.T) {
	for _, c := range []struct {
		mode     string
		set, val bool
		want     bool
	}{{ModeWL, false, false, true}, {ModeLive, false, false, false}, {ModeHandover1s, false, false, false}, {ModeWL, true, false, false}, {ModeHandover1s, true, true, true}} {
		if got := RequireContinuousDefault(c.mode, c.set, c.val); got != c.want {
			t.Fatalf("%v: got %v", c, got)
		}
	}
}

func TestAnalyzeRequiredFor4KLive(t *testing.T) {
	r := newRig(t)
	r.cfg.W, r.cfg.H, r.cfg.ChunkFrames = 3840, 2160, 30
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassUsage || rs != "analyze_required_4k_live" {
		t.Fatalf("4K live rows-only without --analyze engine: %v", err)
	}
	r.cfg.Analyze = true // engine on but no hook wired: refused, never silently skipped
	_, err = Run(r.cfg)
	if c, rs := reason(err); c != ClassUsage || rs != "analyze_hook_missing" {
		t.Fatalf("analyze without hook: %v", err)
	}
}

type jw3TimedSrc struct {
	FrameSource
	last atomic.Int64
}

func (s *jw3TimedSrc) Frame(b []byte) error {
	s.last.Store(time.Now().UnixNano())
	return s.FrameSource.Frame(b)
}

// JW3-01: a kept run with a slow ChunkHook must not block the pacer on jobs<-k.
func TestKeptRunPacerNotBlockedBySlowHook(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames, r.cfg.ChunkFrames, r.cfg.Keep, r.cfg.TTLSeconds = 40, 4, true, 60
	ts := &jw3TimedSrc{FrameSource: r.cfg.Source}
	r.cfg.Source = ts
	unblock := make(chan struct{})
	r.cfg.Hook = func(dir string, row *ChunkRow) error {
		if filepath.Base(dir) == "chunk_0" {
			<-unblock
		}
		return nil
	}
	var ticks atomic.Int64
	r.cfg.Clock = func() int64 { return ticks.Add(1_000_000) }
	r.cfg.Sleep = func(ns int64) { ticks.Add(ns) }
	t0 := time.Now()
	time.AfterFunc(1500*time.Millisecond, func() { close(unblock) })
	res, err := Run(r.cfg)
	if el := time.Duration(ts.last.Load() - t0.UnixNano()); err != nil || el >= 1400*time.Millisecond {
		t.Fatalf("last frame captured %v after start: pacer blocked on jobs<-k with Keep (frames=%d err=%v)", el, res.Frames, err)
	}
}

// JW3-03: the receipt carries the gate verdict, so a receipt-only consumer cannot mistake a refused run for a good one.
func TestReceiptStatusReflectsSegmentGate(t *testing.T) {
	g := newRig(t)
	g.cfg.Frames, g.cfg.ChunkFrames, g.cfg.RequireContinuous = 60, 30, true
	g.src.cost = altOverrun
	if _, err := Run(g.cfg); err == nil {
		t.Fatal("gappy run not refused")
	}
	if st := readReceipt(t, g.out)["status"]; st != "segment_gappy" {
		t.Fatalf("gappy receipt status=%v", st)
	}
	s := newRig(t)
	s.cfg.Mode, s.cfg.Det, s.cfg.Keep, s.cfg.Frames, s.cfg.MinSegmentMs = ModeWL, true, true, 60, MinSegmentMs
	s.src.cost = altOverrun
	if _, err := Run(s.cfg); err == nil {
		t.Fatal("short run not refused")
	}
	if st := readReceipt(t, s.out)["status"]; st != "segment_short" && st != "segment_gappy" {
		t.Fatalf("refused short receipt status=%v", st)
	}
	ok := newRig(t)
	ok.cfg.Frames, ok.cfg.ChunkFrames, ok.cfg.RequireContinuous = 60, 30, true
	if _, err := Run(ok.cfg); err != nil {
		t.Fatal(err)
	}
	if st := readReceipt(t, ok.out)["status"]; st != "ok" {
		t.Fatalf("ok receipt status=%v", st)
	}
}

func chunkDirsLeft(t *testing.T, run string) []string {
	t.Helper()
	ents, _ := os.ReadDir(run)
	var left []string
	for _, e := range ents {
		if e.Name() != "owner.json" && e.Name() != ".lock" {
			left = append(left, e.Name())
		}
	}
	return left
}

// RJ3-01 probe: a hook failure on a kept run is detected after the loop; abort must not close jobs twice (panic), must
// release every chunk (kept ones included: a failed run hands nothing over) and account the RAM back.
func TestKeptRunHookFailureIsGateNotPanic(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames, r.cfg.ChunkFrames, r.cfg.Keep = 8, 4, true
	r.cfg.Hook = func(dir string, row *ChunkRow) error {
		if filepath.Base(dir) == "chunk_1" {
			return errors.New("engine down")
		}
		return nil
	}
	res, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "analyze_failed" {
		t.Fatalf("want gate analyze_failed got %v", err)
	}
	if left := chunkDirsLeft(t, r.run); len(left) != 0 {
		t.Fatalf("kept chunks leaked after failure: %v", left)
	}
	if res == nil || res.ResidentEnd != 0 {
		t.Fatalf("resident not accounted back: %+v", res)
	}
}

// RJ3 probe: a release failure on the LAST chunk (no later openChunk sees it) must still be io chunk_release.
func TestFailedReleaseOfLastChunkRc5(t *testing.T) {
	r := newRig(t)
	last := "chunk_" + strconv.Itoa(int((r.cfg.Frames-1)/int64(r.cfg.ChunkFrames)))
	r.cfg.Release = func(d string) error {
		if filepath.Base(d) == last {
			return errors.New("busy")
		}
		return os.RemoveAll(d)
	}
	_, err := Run(r.cfg)
	if cl, rs := reason(err); cl != ClassIO || rs != "chunk_release" {
		t.Fatalf("want io chunk_release got %v", err)
	}
}

// CF-01: START carries the CLOCK_MONOTONIC t0 of render_index 0 and exists before frame 0 is captured.
func TestStartFileT0BeforeFrame0(t *testing.T) {
	r := newRig(t)
	r.cfg.WriteStart = true
	var seen []byte
	var at0 int64
	inner := r.cfg.Source
	r.cfg.Source = srcFunc(func(dst []byte) error {
		if seen == nil {
			at0 = *r.clk
			seen, _ = os.ReadFile(filepath.Join(r.run, StartFile))
			if seen == nil {
				seen = []byte{}
			}
		}
		return inner.Frame(dst)
	})
	if _, err := Run(r.cfg); err != nil {
		t.Fatal(err)
	}
	var st struct {
		Schema  string `json:"schema"`
		Clock   string `json:"clock"`
		T0      int64  `json:"t0_ns"`
		Delta   int64  `json:"delta_ns"`
		Frames  int64  `json:"frames"`
		W, H    int
		ChunkFr int `json:"chunk_frames"`
	}
	if err := json.Unmarshal(seen, &st); err != nil {
		t.Fatalf("START absent before frame 0: %q %v", seen, err)
	}
	if st.Schema != "os.cu.wlcap-start/v1" || st.Clock != "CLOCK_MONOTONIC" || st.T0 != at0 || st.Delta != FrameNs ||
		st.Frames != r.cfg.Frames || st.W != tw || st.H != th || st.ChunkFr != r.cfg.ChunkFrames {
		t.Fatalf("START %+v (frame0 clock %d)", st, at0)
	}
	fi, err := os.Stat(filepath.Join(r.run, StartFile))
	if err != nil || fi.Mode().Perm()&0o007 != 0 {
		t.Fatalf("START mode %v %v", fi, err)
	}
	dup := newRig(t) // a second run in the same dir must not overwrite another run's t0
	dup.cfg.WriteStart = true
	os.WriteFile(filepath.Join(dup.run, StartFile), []byte("{}"), 0o640)
	if _, err := Run(dup.cfg); err == nil {
		t.Fatal("existing START overwritten")
	} else if cl, rs := reason(err); cl != ClassIO || rs != "start_exists" {
		t.Fatalf("dup START: %v", err)
	}
	off := newRig(t) // WriteStart off: no START (existing run-dir shape unchanged)
	if _, err := Run(off.cfg); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(off.run, StartFile)); err == nil {
		t.Fatal("START written without WriteStart")
	}
}

type srcFunc func([]byte) error

func (f srcFunc) Frame(dst []byte) error { return f(dst) }
