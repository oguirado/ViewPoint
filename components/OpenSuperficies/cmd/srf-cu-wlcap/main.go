// srf-cu-wlcap is the chunked 60 fps capture driver of the CU capsule: it paces an Xvfb_screen0 (or synthetic) XWD
// source at Δ=16,666,667 ns into <ramws>/chunk_<k>/frames.rgb24, releases chunks (<=2 resident) and writes
// path-free receipt.json / rows.jsonl / gaps.jsonl to --out. Exit codes come from src/cubook/rc (0,2..7; never 1).
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/rc"
)

func main() {
	syscall.Umask(0o027) // no world bits on anything this process creates (explicit modes in the driver add the ACL mask)
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// triBool is a bool flag that remembers whether it was set (--require-continuous defaults by mode).
type triBool struct{ set, val bool }

func (t *triBool) String() string   { return fmt.Sprint(t.val) }
func (t *triBool) IsBoolFlag() bool { return true }
func (t *triBool) Set(v string) error {
	switch v {
	case "", "true", "1", "on":
		t.val = true
	case "false", "0", "off":
		t.val = false
	default:
		return fmt.Errorf("bad bool %q", v)
	}
	t.set = true
	return nil
}

func codeOf(err error) int {
	var ce *capture.Error
	if errors.As(err, &ce) {
		if ce.Reason == "E_RAM_NOT_TMPFS" || ce.Reason == "E_RAM_QUOTA" {
			return rc.Code(ce.Reason)
		}
		return rc.Code(ce.Class)
	}
	return rc.Of(err)
}

func run(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("srf-cu-wlcap", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fake := fs.Bool("fake-fb", false, "synthetic in-memory XWD framebuffer (needs OS_CU_TEST=1)")
	fb := fs.String("fb", "", "path of Xvfb_screen0")
	ramDir := fs.String("ramws-dir", "", "ramws run directory (from vision/ramws_shim.py)")
	out := fs.String("out", "", "evidence directory (never under /dev/shm)")
	display := fs.String("display", "", "X display (real mode; only :100 inside the capsule)")
	iso := fs.String("iso", "", "expected VQA_ISO (32 hex, real mode)")
	mode := fs.String("mode", capture.ModeLive, "wl|live|handover-1s|bench (bench: pacing receipt only, never retains, --analyze off)")
	det := fs.String("det", "off", "on|off (on for WL, off for live)")
	w := fs.Int("w", 64, "fake-fb width")
	h := fs.Int("h", 48, "fake-fb height")
	frames := fs.Int64("frames", 0, "render slots to cover (or --seconds)")
	seconds := fs.Int64("seconds", 0, "seconds at 60 fps")
	chunk := fs.Int("chunk", capture.DefaultChunkFrames, "frames per chunk (4K: 30)")
	quota := fs.Int64("quota-bytes", 0, "ramws quota of the run (0 = 2 GiB default)")
	keep := fs.Bool("keep", false, "keep chunks resident until the TTL instead of releasing")
	minSeg := fs.Int64("min-segment-ms", 0, "required continuous segment (WL: 5000)")
	ttl := fs.Int64("ttl-seconds", 0, "handover TTL for kept chunks (0 = 7200 when kept)")
	srcName := fs.String("src", "readat", "frame source: readat|mmap|xshm")
	analyze := fs.String("analyze", "off", "off|engine|host (engine: chunk_analyze.py in-process; host: READY/ANALYZED handshake)")
	waitMs := fs.Int64("analyze-wait-ms", 30000, "--analyze host: bounded wait per chunk for ANALYZED|ANALYZE_FAILED (1..600000)")
	script := fs.String("analyze-script", "vision/chunk_analyze.py", "per-chunk analysis script (--analyze engine)")
	var reqCont triBool
	fs.Var(&reqCont, "require-continuous", "rc 5 segment_gappy if any slot is lost (default on for wl)")
	reap := fs.Bool("reap", false, "release the chunks of --ramws-dir when handover.json's TTL has expired, then exit")
	if err := fs.Parse(args); err != nil {
		fmt.Fprintln(stderr, "E_USAGE", err)
		return rc.Usage
	}
	fail := func(err error) int {
		fmt.Fprintln(stderr, err)
		return codeOf(err)
	}
	usage := func(why string) int { return fail(&capture.Error{Class: capture.ClassUsage, Reason: why}) }
	guard := capture.DefaultGuard()
	if *reap {
		if *ramDir == "" {
			return usage("args")
		}
		if err := guard.CheckRunDir(*ramDir); err != nil {
			return fail(err)
		}
		done, err := capture.ReapExpired(*ramDir, time.Now().Unix())
		if err != nil {
			return fail(err)
		}
		fmt.Fprintf(stdout, "{\"reaped\":%d}\n", b2i(done))
		return rc.OK
	}
	if *det != "on" && *det != "off" {
		return usage("det")
	}
	if *analyze != "off" && *analyze != "engine" && *analyze != "host" {
		return usage("analyze")
	}
	if *waitMs < 1 || *waitMs > int64(capture.MaxHostWait/time.Millisecond) {
		return usage("analyze_wait")
	}
	if *srcName != "readat" && *srcName != "mmap" && *srcName != "xshm" {
		return usage("src")
	}
	n := *frames
	if *seconds > 0 {
		if n != 0 {
			return usage("args")
		}
		n = *seconds * 60
	}
	if n < 1 || *ramDir == "" || *out == "" || (*fake == (*fb != "")) {
		return usage("args")
	}
	nonce := ""
	if *mode != capture.ModeBench {
		if nonce = capture.NewRunNonce(); nonce == "" {
			return fail(&capture.Error{Class: capture.ClassGate, Reason: "run_nonce"}) // JW4-05: no entropy, no unfenced run
		}
	}
	var src capture.FrameSource
	W, H := *w, *h
	if *fake {
		if os.Getenv("OS_CU_TEST") != "1" {
			return usage("fake_requires_test_env")
		}
		switch *srcName {
		case "readat":
			x := capture.SynthXWD(W, H, 0, nil)
			src = &capture.XWDSource{Src: bytesAt(x), Size: int64(len(x))}
		default: // mmap needs a real file, xshm needs a getter: both refuse without them
			if _, err := capture.OpenSource(*srcName, nil, nil); err != nil {
				return fail(err)
			}
		}
	} else {
		if err := capture.GuardDisplay(*display, capture.InCapsule(capture.HostProbe())); err != nil {
			return fail(err)
		}
		xf, conn, err := capture.DialX(*display)
		if err != nil {
			return fail(err)
		}
		defer conn.Close()
		_ = xf
		got, err := capture.ReadIso(conn)
		if err != nil {
			return fail(err)
		}
		if err := capture.GuardIso(*iso, got); err != nil {
			return fail(err)
		}
		f, err := os.Open(*fb)
		if err != nil {
			return fail(&capture.Error{Class: capture.ClassIO, Reason: "fb_open", Err: err})
		}
		defer f.Close()
		hd, err := capture.ParseXWDHeader(f)
		if err != nil {
			return fail(err)
		}
		W, H = hd.Width, hd.Height
		if src, err = capture.OpenSource(*srcName, f, nil); err != nil {
			return fail(err)
		}
		if c, ok := src.(io.Closer); ok {
			defer c.Close()
		}
	}
	cfg := capture.Config{
		Guard: guard, RunDir: *ramDir, OutDir: *out, W: W, H: H, Frames: n, ChunkFrames: *chunk, Mode: *mode,
		Det: *det == "on", Keep: *keep, QuotaBytes: *quota, MinSegmentMs: *minSeg, TTLSeconds: *ttl,
		Clock: monoNs, Sleep: func(ns int64) { time.Sleep(time.Duration(ns)) },
		NowSec: func() int64 { return time.Now().Unix() }, Source: src, SrcName: *srcName,
		Analyze: *analyze != "off", WriteStart: *mode != capture.ModeBench, RunNonce: nonce, RequireContinuous: capture.RequireContinuousDefault(*mode, reqCont.set, reqCont.val),
	}
	switch *analyze {
	case "engine":
		cfg.Hook = (&capture.Analyzer{W: W, H: H, Run: capture.ExecRunner(*script, 0)}).Hook()
	case "host":
		cfg.Hook = (&capture.HostAnalyzer{W: W, H: H, RunNonce: cfg.RunNonce, Wait: time.Duration(*waitMs) * time.Millisecond}).Hook()
	}
	res, err := capture.Run(cfg)
	if err != nil {
		return fail(err)
	}
	b, _ := json.Marshal(map[string]any{"published": res.Published, "frames": res.Frames, "overrun": res.Overrun,
		"dropped": res.Dropped, "segment_ms": res.SegmentMs, "span_ms": res.SpanMs, "longest_run_ms": res.LongestRunMs, "chunks": len(res.Chunks), "gaps": len(res.Gaps),
		"peak_resident_bytes": res.PeakResident})
	fmt.Fprintln(stdout, string(b))
	return rc.OK
}

func b2i(v bool) int {
	n := 0
	if v {
		n++
	}
	return n
}

type bytesAt []byte

func (b bytesAt) ReadAt(p []byte, off int64) (int, error) {
	if off >= int64(len(b)) {
		return 0, io.EOF
	}
	return copy(p, b[off:]), nil
}

// monoNs is CLOCK_MONOTONIC in ns: the pacer clock and the START t0 the actuator shares in the same kernel (CF-01).
func monoNs() int64 {
	var ts unix.Timespec
	if unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts) != nil {
		return 0
	}
	return ts.Nano()
}
