// srf-cu-snap captures raw RGB24 frames from the CU capsule framebuffer (or a synthetic one) into a
// ramws directory handed in by the caller and writes path-free evidence JSON to --out.
// Exit codes come from src/cubook/rc (0,2..7; never 1).
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/rc"
)

func main() { os.Exit(run(os.Args[1:], os.Stdout, os.Stderr)) }

func codeOf(err error) int {
	var ce *capture.Error
	if errors.As(err, &ce) {
		if ce.Reason == "E_RAM_NOT_TMPFS" {
			return rc.Code("E_RAM_NOT_TMPFS")
		}
		return rc.Code(ce.Class)
	}
	return rc.Of(err)
}

func writeJSON(path string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(b, '\n'), 0o640)
}

func run(args []string, stdout, stderr io.Writer) int {
	syscall.Umask(0o027) // no world bits on anything this process creates (AW3-07, ADC-W3-06)
	fs := flag.NewFlagSet("srf-cu-snap", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fake := fs.Bool("fake-fb", false, "use a synthetic in-memory XWD framebuffer")
	fb := fs.String("fb", "", "path of Xvfb_screen0 (mmap)")
	n := fs.Int("frames", 1, "frames to capture (1..64)")
	ramDir := fs.String("ramws-dir", "", "ramws run directory for frames.raw (from vision/ramws_shim.py)")
	out := fs.String("out", "", "evidence directory (manifest + receipt; never under /dev/shm)")
	display := fs.String("display", "", "X display (real mode; only :100 inside the capsule)")
	iso := fs.String("iso", "", "expected VQA_ISO (32 hex, real mode)")
	app := fs.String("app", "cockpit", "manifest scope.app")
	scene := fs.String("scene", "idle", "manifest scope.scene")
	if err := fs.Parse(args); err != nil {
		fmt.Fprintln(stderr, "E_USAGE", err)
		return rc.Usage
	}
	fail := func(err error) int {
		fmt.Fprintln(stderr, err)
		return codeOf(err)
	}
	usage := func(why string) int { return fail(&capture.Error{Class: capture.ClassUsage, Reason: why}) }
	if *n < 1 || *n > 64 || *ramDir == "" || *out == "" || (*fake == (*fb != "")) {
		return usage("args")
	}
	if *fake && os.Getenv("OS_CU_TEST") != "1" {
		return usage("fake_requires_test_env")
	}
	if err := capture.CheckOut(*out, capture.MountFSType); err != nil {
		return fail(err)
	}
	guard := capture.DefaultGuard()
	if err := guard.CheckRunDir(*ramDir); err != nil {
		return fail(err)
	}
	var src io.ReaderAt
	var fz capture.Freezer = noFreeze{}
	if *fake {
		src = bytesAt(capture.SynthXWD(64, 48, 0, nil))
	} else {
		if err := capture.GuardDisplay(*display, capture.InCapsule(capture.HostProbe())); err != nil {
			return fail(err)
		}
		xf, conn, err := capture.DialX(*display)
		if err != nil {
			return fail(err)
		}
		defer conn.Close()
		got, err := capture.ReadIso(conn)
		if err != nil {
			return fail(err)
		}
		if err := capture.GuardIso(*iso, got); err != nil {
			return fail(err)
		}
		mm, err := capture.MmapFile(*fb)
		if err != nil {
			return fail(err)
		}
		defer mm.Close()
		src, fz = mm, xf
	}
	h, err := capture.ParseXWDHeader(src)
	if err != nil {
		return fail(err)
	}
	if err := os.MkdirAll(*out, 0o750); err != nil {
		return fail(&capture.Error{Class: capture.ClassIO, Reason: "out_mkdir", Err: err})
	}
	ring := capture.NewRing(*ramDir, h.Width, h.Height)
	defer ring.Close()
	sn := &capture.Snapper{Src: src, Freezer: fz, Ring: ring, Watchdog: capture.DefaultWatchdog}
	for i := 0; i < *n; i++ {
		if _, err := sn.Snap(); err != nil {
			return fail(err)
		}
	}
	if _, err := ring.WriteRawJSON(); err != nil {
		return fail(err)
	}
	peak := ring.SlotBytes() + int64(h.Width*h.Height*3)
	rec := ring.Receipt(h, guard.FS(*ramDir), peak)
	if err := writeJSON(filepath.Join(*out, "frames.manifest.json"), ring.Manifest(*app, *scene)); err != nil {
		return fail(&capture.Error{Class: capture.ClassIO, Reason: "manifest_write", Err: err})
	}
	if err := writeJSON(filepath.Join(*out, "capture_receipt.json"), rec); err != nil {
		return fail(&capture.Error{Class: capture.ClassIO, Reason: "receipt_write", Err: err})
	}
	b, _ := json.Marshal(rec)
	fmt.Fprintln(stdout, string(b))
	return rc.OK
}

type noFreeze struct{}

func (noFreeze) Freeze() error { return nil }
func (noFreeze) Thaw() error   { return nil }

type bytesAt []byte

func (b bytesAt) ReadAt(p []byte, off int64) (int, error) {
	if off >= int64(len(b)) {
		return 0, io.EOF
	}
	n := copy(p, b[off:])
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}
