package capture

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func benchRig(t *testing.T) *rig {
	r := newRig(t)
	r.cfg.Mode = ModeBench
	r.cfg.SrcName = "readat"
	return r
}

func benchLeft(t *testing.T, run string) []string {
	t.Helper()
	ents, err := os.ReadDir(run)
	if err != nil {
		t.Fatal(err)
	}
	var left []string
	for _, e := range ents {
		n := e.Name()
		if strings.HasPrefix(n, "chunk_") || n == StartFile || n == "handover.json" || n == StartFile+".tmp" {
			left = append(left, n)
		}
	}
	return left
}

// AW4-03: bench never retains: no keep, no handover, no START, nothing resident at exit.
func TestBenchNeverKeeps(t *testing.T) {
	r := benchRig(t)
	r.cfg.Keep = true
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassUsage || rs != "keep_not_allowed_bench" {
		t.Fatalf("bench --keep: %v", err)
	}
	r = benchRig(t)
	r.cfg.WriteStart = true // the driver forces it off in bench
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if left := benchLeft(t, r.run); len(left) != 0 {
		t.Fatalf("bench left %v", left)
	}
	if res.ResidentEnd != 0 {
		t.Fatalf("resident_end %d", res.ResidentEnd)
	}
}

func TestBenchRefusesAnalyze(t *testing.T) {
	r := benchRig(t)
	r.cfg.Analyze = true
	r.cfg.Hook = func(string, *ChunkRow) error { return nil }
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassUsage || rs != "analyze_not_allowed_bench" {
		t.Fatalf("bench analyze: %v", err)
	}
}

// Two sequential bench runs on one RunDir (the pace4k 18-run session) both succeed and leave it empty.
func TestBenchReleasesAll(t *testing.T) {
	r := benchRig(t)
	for i := 0; i < 2; i++ {
		*r.clk = 1_000_000_000
		res, err := Run(r.cfg)
		if err != nil {
			t.Fatalf("run %d: %v", i, err)
		}
		if res.ResidentEnd != 0 || len(benchLeft(t, r.run)) != 0 {
			t.Fatalf("run %d resident=%d left=%v", i, res.ResidentEnd, benchLeft(t, r.run))
		}
		rc := readReceipt(t, r.out)
		if rc["schema"] != "os.cu.wlcap-bench/v1" || num(rc, "resident_end_bytes") != 0 {
			t.Fatalf("receipt %v", rc)
		}
	}
}

func TestBench4KNoAnalyze(t *testing.T) {
	r := benchRig(t)
	r.cfg.W, r.cfg.H, r.cfg.Frames, r.cfg.ChunkFrames = 3840, 2160, 4, 2
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatalf("4K bench without analyze: %v", err)
	}
	rc := readReceipt(t, r.out)
	if res.Frames != 4 || rc["schema"] != "os.cu.wlcap-bench/v1" || num(rc, "frames") != 4 || num(rc, "peak_resident_bytes") <= 0 {
		t.Fatalf("receipt %v", rc)
	}
	if left := benchLeft(t, r.run); len(left) != 0 {
		t.Fatalf("left %v", left)
	}
}

// SW-0167: x(k), k = max(1, ceil(ppm*n/1e6)) on integer ns, reported in us.
func TestBenchXkPercentiles(t *testing.T) {
	var ns []int64
	for i := int64(1); i <= 10; i++ {
		ns = append(ns, i*1000*(11-i)) // unsorted: 10000,18000,24000,28000,30000,30000,28000,24000,18000,10000
	}
	cases := []struct{ ppm, want int64 }{{500000, 24}, {950000, 30}, {1, 10}, {1000000, 30}, {0, 10}}
	for _, c := range cases {
		if got := xkUs(ns, c.ppm); got != c.want {
			t.Fatalf("ppm %d: got %d want %d", c.ppm, got, c.want)
		}
	}
	if xkUs(nil, 500000) != 0 {
		t.Fatal("empty")
	}
}

func TestBenchConservation(t *testing.T) {
	r := benchRig(t)
	r.src.cost, r.src.clk = altOverrun, r.clk
	res, err := Run(r.cfg)
	if err != nil {
		t.Fatal(err)
	}
	rc := readReceipt(t, r.out)
	req, fr, ov, dr, pub := num(rc, "requested"), num(rc, "frames"), num(rc, "overrun"), num(rc, "dropped"), num(rc, "published")
	if ov == 0 || fr == 0 || req != r.cfg.Frames || req != fr+ov+dr || pub != req || res.Published != uint64(pub) {
		t.Fatalf("conservation req=%d frames=%d overrun=%d dropped=%d published=%d", req, fr, ov, dr, pub)
	}
	for _, k := range []string{"src", "det", "gaps", "span_ms", "longest_run_ms", "capture_us_p50", "capture_us_p95", "write_us_per_frame", "peak_resident_bytes", "resident_end_bytes", "status"} {
		if _, ok := rc[k]; !ok {
			t.Fatalf("receipt lacks %s: %v", k, rc)
		}
	}
	if rc["status"] != "ok" {
		t.Fatalf("status %v", rc["status"])
	}
}

// Live/wl/handover rules unchanged by the new mode, and bench is accepted.
func TestLiveRulesUnchanged(t *testing.T) {
	r := newRig(t)
	r.cfg.Det = true
	if _, err := Run(r.cfg); true {
		if c, rs := reason(err); c != ClassUsage || rs != "det_not_allowed_live" {
			t.Fatalf("live det: %v", err)
		}
	}
	r = newRig(t)
	r.cfg.W, r.cfg.H, r.cfg.ChunkFrames = 3840, 2160, 30
	if _, err := Run(r.cfg); true {
		if c, rs := reason(err); c != ClassUsage || rs != "analyze_required_4k_live" {
			t.Fatalf("4K live: %v", err)
		}
	}
	r = newRig(t)
	r.cfg.Mode, r.cfg.Det = ModeWL, false
	if _, err := Run(r.cfg); true {
		if c, rs := reason(err); c != ClassUsage || rs != "det_required_wl" {
			t.Fatalf("wl: %v", err)
		}
	}
	r = newRig(t)
	r.cfg.Mode, r.cfg.W, r.cfg.H = ModeHandover1s, 64, 48
	if _, err := Run(r.cfg); true {
		if c, rs := reason(err); c != ClassUsage || rs != "handover_1s_res" {
			t.Fatalf("handover: %v", err)
		}
	}
	r = benchRig(t) // bench accepted, det either way
	r.cfg.Det = true
	res, err := Run(r.cfg)
	if err != nil || len(res.Chunks) == 0 || len(res.Chunks[0].Det) == 0 {
		t.Fatalf("bench det on: %v %+v", err, res)
	}
	rc := readReceipt(t, r.out)
	if rc["schema"] != "os.cu.wlcap-bench/v1" {
		t.Fatalf("receipt schema %v", rc["schema"])
	}
}

var nonceRe = regexp.MustCompile(`^[0-9a-f]{32}$`)

func TestNewRunNonce(t *testing.T) {
	a, b := NewRunNonce(), NewRunNonce()
	if !nonceRe.MatchString(a) || a == b {
		t.Fatalf("nonce %q %q", a, b)
	}
}

// AW4-02: START carries the 128-bit run_nonce, written O_EXCL|O_NOFOLLOW, 0o640.
func TestStartWritesRunNonce(t *testing.T) {
	r := newRig(t)
	r.cfg.WriteStart = true
	r.cfg.RunNonce = "00112233445566778899aabbccddeeff"
	var seen []byte
	inner := r.cfg.Source
	r.cfg.Source = srcFunc(func(dst []byte) error {
		if seen == nil {
			seen, _ = os.ReadFile(filepath.Join(r.run, StartFile))
		}
		return inner.Frame(dst)
	})
	if _, err := Run(r.cfg); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(seen), `"run_nonce":"00112233445566778899aabbccddeeff"`) {
		t.Fatalf("START %q", seen)
	}
	r = newRig(t) // empty nonce: Run generates one
	r.cfg.WriteStart = true
	r.cfg.Source = srcFunc(func(dst []byte) error {
		if seen = nil; true {
			seen, _ = os.ReadFile(filepath.Join(r.run, StartFile))
		}
		return nil
	})
	_, _ = Run(r.cfg)
	if m := regexp.MustCompile(`"run_nonce":"([0-9a-f]{32})"`).FindSubmatch(seen); m == nil {
		t.Fatalf("generated nonce missing: %q", seen)
	}
}

// A symlink named START is not followed (O_NOFOLLOW) and the writer refuses it.
func TestStartRefusesSymlink(t *testing.T) {
	r := newRig(t)
	r.cfg.WriteStart = true
	tgt := filepath.Join(t.TempDir(), "victim")
	if err := os.Symlink(tgt, filepath.Join(r.run, StartFile)); err != nil {
		t.Fatal(err)
	}
	_, err := Run(r.cfg)
	if err == nil {
		t.Fatal("symlink START accepted")
	}
	if _, serr := os.Lstat(tgt); serr == nil {
		t.Fatal("symlink target created")
	}
}
