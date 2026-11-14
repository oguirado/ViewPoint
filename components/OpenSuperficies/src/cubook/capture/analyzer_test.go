package capture

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"opensuperficies.local/srf/src/cubook/rc"
)

func okJSON(frames, grid int, us int64, each int64) []byte {
	return []byte(fmt.Sprintf(`{"chunk":0,"frames":%d,"grid":%d,"class_counts":{"UNCHANGED":%d,"JITTER":0,"CHANGED":0,"MOVED":0,"FLICKER":0,"FROZEN":0,"DRIFT":0},"analyze_us_per_frame":%d,"engine_wheel_sha8":"5e2395d5"}`+"\n", frames, grid, each, us))
}

func fixedRun(out []byte, err error) AnalyzeRunner {
	return func(string, int, int, int, int) ([]byte, error) { return out, err }
}

func chunkDirWithFrames(t *testing.T) string {
	t.Helper()
	d := filepath.Join(t.TempDir(), "chunk_0")
	if err := os.Mkdir(d, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(d, "frames.rgb24"), []byte{1, 2, 3}, 0o640); err != nil {
		t.Fatal(err)
	}
	return d
}

func TestGridForMatchesEngine(t *testing.T) {
	for _, c := range []struct{ w, h, s int }{{64, 48, 16}, {1280, 720, 16}, {1920, 1080, 32}, {3840, 2160, 64}, {0, 5, 0}} {
		if g := GridFor(c.w, c.h); g != c.s {
			t.Errorf("GridFor(%d,%d)=%d want %d", c.w, c.h, g, c.s)
		}
	}
}

// The chunk stays leased until the analysis returned; only then is it released (SW-0148 I3).
func TestAnalyzerFailureReleasesChunkRc5(t *testing.T) {
	d := chunkDirWithFrames(t)
	bad := map[string]AnalyzeRunner{
		"runner_error":  fixedRun(nil, errors.New("exit 5")),
		"garbage":       fixedRun([]byte("not json"), nil),
		"unknown_field": fixedRun([]byte(`{"chunk":0,"x":1}`), nil),
		"frames_differ": fixedRun(okJSON(9, 32, 5, 1), nil),
		"grid_differs":  fixedRun(okJSON(3, 16, 5, 1), nil),
		"zero_us":       fixedRun(okJSON(3, 32, 0, 1), nil),
	}
	for name, run := range bad {
		a := &Analyzer{W: 1920, H: 1080, Run: run}
		row := &ChunkRow{Frames: 3}
		err := a.Analyze(d, row)
		var ce *Error
		if !errors.As(err, &ce) || ce.Class != ClassGate || ce.Reason != "analyze_failed" || rc.Code(ce.Class) != 5 {
			t.Errorf("%s: err=%v", name, err)
		}
		if row.AnalyzeRC != 5 || row.AnalyzeUsPerF != 0 || a.Totals().Failed != 1 {
			t.Errorf("%s: row=%+v totals=%+v", name, row, a.Totals())
		}
	}
	// the chunk is still releasable after a failed analysis, and the analyzer keeps working
	if err := os.RemoveAll(d); err != nil {
		t.Fatal(err)
	}
	a := &Analyzer{W: 1920, H: 1080, Run: fixedRun(okJSON(3, 32, 7, 1), nil)}
	if err := a.Analyze(d, &ChunkRow{Frames: 3}); err != nil {
		t.Fatalf("positive control: %v", err)
	}
}

func TestAnalyzeUsPerFrameFilled(t *testing.T) {
	a := &Analyzer{W: 3840, H: 2160, Run: fixedRun(okJSON(30, 64, 21689, 4000), nil)}
	row := &ChunkRow{Frames: 30}
	if err := a.Analyze("d", row); err != nil {
		t.Fatal(err)
	}
	if row.AnalyzeUsPerF != 21689 || row.Grid != 64 || row.EngineWheelSha8 != "5e2395d5" || row.AnalyzeRC != 0 || row.ClassCounts["UNCHANGED"] != 4000 {
		t.Fatalf("row=%+v", row)
	}
	raw, err := marshalRow(row)
	if err != nil || !contains(raw, `"analyze_us_per_frame":21689`) || !contains(raw, `"grid":64`) {
		t.Fatalf("json=%s err=%v", raw, err)
	}
}

func TestAnalyzeUs64BitSum(t *testing.T) {
	const us = int64(3_000_000_000) // > 2^31: a 32-bit accumulator wraps
	a := &Analyzer{W: 1920, H: 1080, Run: fixedRun(okJSON(2, 32, us, 3_000_000_000), nil)}
	for i := 0; i < 2; i++ {
		if err := a.Analyze("d", &ChunkRow{Frames: 2}); err != nil {
			t.Fatal(err)
		}
	}
	if got, want := a.Totals().UsTotal, 2*2*us; got != want || got <= 1<<32 {
		t.Fatalf("UsTotal=%d want %d", got, want)
	}
	if tt := a.Totals(); tt.Chunks != 2 || tt.Frames != 4 {
		t.Fatalf("totals=%+v", tt)
	}
}

func TestAnalyzeFlagNoRc1(t *testing.T) {
	// cmd/srf-cu-wlcap maps a capture.Error through rc.Code(Class)
	code := func(err error) int {
		var ce *Error
		if errors.As(err, &ce) {
			return rc.Code(ce.Class)
		}
		return rc.Of(err)
	}
	okA := &Analyzer{W: 64, H: 48, Run: fixedRun(okJSON(2, 16, 5, 1), nil)}
	if got := code(okA.Analyze("d", &ChunkRow{Frames: 2})); got != 0 {
		t.Fatalf("success rc=%d", got)
	}
	for name, a := range map[string]*Analyzer{
		"no_runner": {W: 64, H: 48},
		"bad_geom":  {W: 0, H: 48, Run: fixedRun(okJSON(2, 16, 5, 1), nil)},
		"fails":     {W: 64, H: 48, Run: fixedRun(nil, errors.New("x"))},
	} {
		got := code(a.Analyze("d", &ChunkRow{Frames: 2}))
		if got != 5 || got == 1 {
			t.Errorf("%s rc=%d want 5", name, got)
		}
	}
	if rc.Code("E_RAM_NOT_TMPFS") != 5 || rc.Code("E_GRID") != 2 || rc.Code("E_ANALYZE") != 5 {
		t.Fatal("rc table")
	}
}

func marshalRow(r *ChunkRow) ([]byte, error) { return json.Marshal(r) }

func contains(b []byte, s string) bool { return bytes.Contains(b, []byte(s)) }

// ADC-W3-07: the engine's chunk index must equal the row it fills.
func TestAnalyzerRejectsChunkIndexMismatch(t *testing.T) {
	a := &Analyzer{W: 1920, H: 1080, Run: func(_ string, _, _, n, g int) ([]byte, error) { return okJSON(n, g, 10, 1), nil }}
	row := &ChunkRow{Chunk: 3, Frames: 2} // okJSON reports chunk 0
	if err := a.Analyze("d", row); err == nil || row.AnalyzeRC != 5 {
		t.Fatalf("chunk index mismatch accepted: err=%v rc=%d", err, row.AnalyzeRC)
	}
	ok := &ChunkRow{Chunk: 0, Frames: 2}
	if err := a.Analyze("d", ok); err != nil || ok.AnalyzeRC != 0 {
		t.Fatalf("matching chunk refused: %v", err)
	}
}

// fakeHost plays bench/wl/driver.py: it waits for chunk_<k>/READY and answers with answer(k, ready) through a tmp+rename.
func fakeHost(t *testing.T, run string, answer func(k int, ready map[string]any) (name string, body []byte)) (stop func() map[int]map[string]any) {
	t.Helper()
	done := make(chan struct{})
	seen := map[int]map[string]any{}
	fin := make(chan struct{})
	go func() {
		defer close(fin)
		for i := 0; i < 20000; i++ { // bounded: 20000 x 0.5 ms
			select {
			case <-done:
				return
			default:
			}
			ents, _ := os.ReadDir(run)
			for _, e := range ents {
				var k int
				if _, err := fmt.Sscanf(e.Name(), "chunk_%d", &k); err != nil || seen[k] != nil {
					continue
				}
				b, err := os.ReadFile(filepath.Join(run, e.Name(), ReadyFile))
				if err != nil {
					continue
				}
				var m map[string]any
				if json.Unmarshal(b, &m) != nil {
					continue
				}
				seen[k] = m
				name, body := answer(k, m)
				if name == "" {
					continue
				}
				d := filepath.Join(run, e.Name())
				os.WriteFile(filepath.Join(d, name+".tmp"), body, 0o640)
				os.Rename(filepath.Join(d, name+".tmp"), filepath.Join(d, name))
			}
			time.Sleep(500 * time.Microsecond)
		}
	}()
	return func() map[int]map[string]any { close(done); <-fin; return seen }
}

func hostRow(k, frames, grid int) []byte {
	return []byte(fmt.Sprintf(`{"chunk":%d,"frames":%d,"grid":%d,"class_counts":{"UNCHANGED":%d,"JITTER":0,"CHANGED":1,"MOVED":0,"FLICKER":0,"FROZEN":0,"DRIFT":0},"analyze_us_per_frame":11,"engine_wheel_sha8":"5e2395d5"}`+"\n", k, frames, grid, frames*12-1))
}

// CF-02 capsule side: READY -> host ANALYZED -> row filled from ANALYZED and its sha, release strictly after the marker.
func TestHostHandshakeReadyAnalyzedThenRelease(t *testing.T) {
	r := newRig(t)
	r.cfg.Mode, r.cfg.Det, r.cfg.Analyze = ModeWL, true, true
	r.cfg.Hook = (&HostAnalyzer{W: tw, H: th, Wait: 5 * time.Second}).Hook()
	sent := map[int][]byte{}
	var mu sync.Mutex
	stop := fakeHost(t, r.run, func(k int, m map[string]any) (string, []byte) {
		b := hostRow(k, int(m["frames"].(float64)), GridFor(tw, th))
		mu.Lock()
		sent[k] = b
		mu.Unlock()
		return AnalyzedFile, b
	})
	var bad []string
	r.cfg.Release = func(d string) error {
		if _, err := os.Stat(filepath.Join(d, AnalyzedFile)); err != nil {
			bad = append(bad, filepath.Base(d))
		}
		return os.RemoveAll(d)
	}
	res, err := Run(r.cfg)
	seen := stop()
	if err != nil {
		t.Fatal(err)
	}
	if len(bad) != 0 || len(res.Chunks) != 5 || len(seen) != 5 {
		t.Fatalf("released before ANALYZED %v chunks=%d ready=%d", bad, len(res.Chunks), len(seen))
	}
	for _, c := range res.Chunks {
		m := seen[c.Chunk]
		if m["schema"] != "os.cu.chunk-ready/v1" || int(m["chunk"].(float64)) != c.Chunk || int(m["frames"].(float64)) != c.Frames ||
			int(m["w"].(float64)) != tw || int(m["h"].(float64)) != th || int(m["grid"].(float64)) != GridFor(tw, th) ||
			uint64(m["first_render"].(float64)) != c.FirstRender || len(m["det_sha256"].([]any)) != c.Frames {
			t.Fatalf("READY %d = %v", c.Chunk, m)
		}
		s := sha256.Sum256(sent[c.Chunk])
		if c.AnalyzeRC != 0 || c.ClassCounts["CHANGED"] != 1 || c.AnalyzeUsPerF != 11 || c.AnalyzedSha != hex.EncodeToString(s[:]) || c.AnalyzeTimeout != 0 {
			t.Fatalf("row %+v", c)
		}
	}
}

// ANALYZE_FAILED from the host is rc 5 analyze_failed and every chunk is still released (AW3-02: no RAM leak).
func TestHostAnalyzeFailedReleasesRc5(t *testing.T) {
	r := newRig(t)
	r.cfg.Analyze = true
	r.cfg.Hook = (&HostAnalyzer{W: tw, H: th, Wait: 5 * time.Second}).Hook()
	stop := fakeHost(t, r.run, func(k int, m map[string]any) (string, []byte) {
		if k == 1 {
			return AnalyzeFailedFile, []byte(`{"chunk":1,"rc":5,"reason":"E_PIN"}`)
		}
		return AnalyzedFile, hostRow(k, int(m["frames"].(float64)), GridFor(tw, th))
	})
	res, err := Run(r.cfg)
	stop()
	if c, rs := reason(err); c != ClassGate || rs != "analyze_failed" {
		t.Fatalf("want analyze_failed got %v", err)
	}
	if ents, _ := os.ReadDir(r.run); len(ents) != 2 || res.ResidentEnd != 0 {
		t.Fatalf("chunks leaked: %v resident=%d", ents, res.ResidentEnd)
	}
}

// A host that never answers costs a bounded wait per chunk: the run finishes, the rows say analyze_timeout, the pacer
// records overrun instead of stalling, and at most two chunks are ever resident.
func TestHostNoAnswerBoundedWaitNoStall(t *testing.T) {
	r := newRig(t)
	r.cfg.Frames, r.cfg.ChunkFrames, r.cfg.Analyze = 40, 4, true
	r.cfg.Hook = (&HostAnalyzer{W: tw, H: th, Wait: 40 * time.Millisecond}).Hook()
	var ticks atomic.Int64
	r.cfg.Clock = func() int64 { return ticks.Add(1_000_000) }
	r.cfg.Sleep = func(ns int64) { ticks.Add(ns) }
	t0 := time.Now()
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if time.Since(t0) > 10*time.Second || res.Overrun == 0 || r.src.maxCh > 2 || len(res.Chunks) == 0 {
		t.Fatalf("stall: wall=%v overrun=%d maxCh=%d chunks=%d", time.Since(t0), res.Overrun, r.src.maxCh, len(res.Chunks))
	}
	for _, c := range res.Chunks {
		if c.AnalyzeTimeout != 1 || c.AnalyzeRC != 4 || c.ClassCounts != nil {
			t.Fatalf("row %+v", c)
		}
	}
	m := map[string]any{}
	b, _ := os.ReadFile(filepath.Join(r.out, "receipt.json"))
	json.Unmarshal(b, &m)
	if m["analyze_timeouts"] != float64(len(res.Chunks)) {
		t.Fatalf("receipt analyze_timeouts=%v chunks=%d", m["analyze_timeouts"], len(res.Chunks))
	}
	if ents, _ := os.ReadDir(r.run); len(ents) != 2 {
		t.Fatalf("chunks leaked: %v", ents)
	}
}

// A symlinked (or oversized) ANALYZED is never followed: fail closed as analyze_failed.
func TestHostAnalyzedSymlinkRefused(t *testing.T) {
	r := newRig(t)
	r.cfg.Analyze = true
	r.cfg.Hook = (&HostAnalyzer{W: tw, H: th, Wait: 5 * time.Second}).Hook()
	good := filepath.Join(t.TempDir(), "row.json")
	stop := fakeHost(t, r.run, func(k int, m map[string]any) (string, []byte) {
		if k == 0 {
			os.WriteFile(good, hostRow(0, int(m["frames"].(float64)), GridFor(tw, th)), 0o640)
			os.Symlink(good, filepath.Join(r.run, "chunk_0", AnalyzedFile))
			return "", nil
		}
		return AnalyzedFile, hostRow(k, int(m["frames"].(float64)), GridFor(tw, th))
	})
	_, err := Run(r.cfg)
	stop()
	if c, rs := reason(err); c != ClassGate || rs != "analyze_failed" {
		t.Fatalf("symlinked ANALYZED accepted: %v", err)
	}
}
