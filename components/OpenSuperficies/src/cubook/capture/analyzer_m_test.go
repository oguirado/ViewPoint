package capture

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// W4 lane M: hardened host-marker reader, run_nonce replay fencing, stale ANALYZED refusal (AW4-02/06).

const mNonce = "0123456789abcdef0123456789abcdef"

func mReadErr(t *testing.T, p string, max int64) (b []byte, err error) {
	t.Helper()
	done := make(chan struct{})
	go func() { b, _, err = openMarker(p, max); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatalf("openMarker(%s) blocked > 2 s", filepath.Base(p))
	}
	return b, err
}

func TestMarkerFifoNoBlock(t *testing.T) {
	p := filepath.Join(t.TempDir(), "ANALYZED")
	if err := syscall.Mkfifo(p, 0o600); err != nil {
		t.Skip("no fifo:", err)
	}
	_, err := mReadErr(t, p, 4096)
	if err == nil || errors.Is(err, os.ErrNotExist) {
		t.Fatalf("FIFO must be refused (not absent): %v", err)
	}
	// the analyzer treats a FIFO named ANALYZED as analyze_failed rc 5, bounded
	d := t.TempDir()
	if err := syscall.Mkfifo(filepath.Join(d, AnalyzedFile), 0o600); err != nil {
		t.Skip(err)
	}
	row := &ChunkRow{Chunk: 0, Frames: 2}
	// pre-existing marker before READY is already refused (AW4-06); rc 5 either way
	done := make(chan error, 1)
	go func() { done <- (&HostAnalyzer{W: tw, H: th, Wait: 200 * time.Millisecond}).Analyze(d, row) }()
	select {
	case err := <-done:
		if err == nil || row.AnalyzeRC != 5 {
			t.Fatalf("FIFO ANALYZED: err=%v rc=%d", err, row.AnalyzeRC)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Analyze blocked on FIFO")
	}
}

func TestMarkerSymlinkOversize(t *testing.T) {
	d := t.TempDir()
	good := filepath.Join(d, "good")
	os.WriteFile(good, []byte("abc"), 0o640)
	b, err := mReadErr(t, good, 16)
	if err != nil || string(b) != "abc" {
		t.Fatalf("good marker: %q %v", b, err)
	}
	if _, err := mReadErr(t, filepath.Join(d, "missing"), 16); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("ENOENT must be absent, got %v", err)
	}
	ln := filepath.Join(d, "ln")
	os.Symlink(good, ln)
	if _, err := mReadErr(t, ln, 16); err == nil || errors.Is(err, os.ErrNotExist) {
		t.Fatalf("symlink followed: %v", err)
	}
	big := filepath.Join(d, "big")
	os.WriteFile(big, []byte(strings.Repeat("x", 17)), 0o640)
	if _, err := mReadErr(t, big, 16); err == nil {
		t.Fatal("oversize accepted")
	}
	empty := filepath.Join(d, "empty")
	os.WriteFile(empty, nil, 0o640)
	if _, err := mReadErr(t, empty, 16); err == nil {
		t.Fatal("empty accepted")
	}
	hl := filepath.Join(d, "hl")
	os.Link(good, hl)
	if _, err := mReadErr(t, hl, 16); err == nil {
		t.Fatal("hardlinked marker (st_nlink 2) accepted")
	}
}

// mHost answers READY of chunk 0 in dir with name/body (built from the READY it read); mtime shifts the marker mtime.
func mHost(t *testing.T, dir string, build func(ready map[string]any) (string, []byte), mtime time.Time) func() {
	t.Helper()
	fin := make(chan struct{})
	go func() {
		defer close(fin)
		for i := 0; i < 4000; i++ {
			b, err := os.ReadFile(filepath.Join(dir, ReadyFile))
			if err == nil {
				var m map[string]any
				if jsonUnmarshal(b, &m) == nil {
					name, body := build(m)
					tmp := filepath.Join(dir, name+".h")
					os.WriteFile(tmp, body, 0o640)
					if !mtime.IsZero() {
						os.Chtimes(tmp, mtime, mtime)
					}
					os.Rename(tmp, filepath.Join(dir, name))
					return
				}
			}
			time.Sleep(time.Millisecond)
		}
	}()
	return func() { <-fin }
}

func mRow(nonce string, chunk int) []byte {
	r := string(hostRow(chunk, 2, GridFor(tw, th)))
	if nonce != "" {
		r = strings.Replace(r, `"engine_wheel_sha8"`, fmt.Sprintf(`"run_nonce":"%s","engine_wheel_sha8"`, nonce), 1)
	}
	return []byte(r)
}

func mAnalyze(t *testing.T, ha *HostAnalyzer, dir string) (*ChunkRow, error) {
	t.Helper()
	row := &ChunkRow{Chunk: 0, Frames: 2, FirstRender: 0}
	return row, ha.Analyze(dir, row)
}

func TestAnalyzedWrongNonceRc5(t *testing.T) {
	// echo of the right nonce is accepted and READY carries the nonce
	d := t.TempDir()
	var gotNonce any
	wait := mHost(t, d, func(m map[string]any) (string, []byte) {
		gotNonce = m["run_nonce"]
		return AnalyzedFile, mRow(mNonce, 0)
	}, time.Time{})
	row, err := mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 3 * time.Second, RunNonce: mNonce}, d)
	wait()
	if gotNonce != mNonce {
		t.Fatalf("READY run_nonce = %v", gotNonce)
	}
	if err != nil || row.AnalyzeRC != 0 {
		t.Fatalf("matching nonce refused: %v rc=%d", err, row.AnalyzeRC)
	}
	// wrong nonce, missing nonce: rc 5 marker_replay
	for _, n := range []string{"ffffffffffffffffffffffffffffffff", ""} {
		d := t.TempDir()
		wait := mHost(t, d, func(m map[string]any) (string, []byte) { return AnalyzedFile, mRow(n, 0) }, time.Time{})
		row, err := mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 3 * time.Second, RunNonce: mNonce}, d)
		wait()
		if err == nil || row.AnalyzeRC != 5 {
			t.Fatalf("nonce %q accepted: err=%v rc=%d", n, err, row.AnalyzeRC)
		}
	}
	// ANALYZE_FAILED with the wrong nonce does not get to choose the rc
	d2 := t.TempDir()
	wait2 := mHost(t, d2, func(m map[string]any) (string, []byte) {
		return AnalyzeFailedFile, []byte(`{"chunk":0,"rc":3,"reason":"x","run_nonce":"ffffffffffffffffffffffffffffffff"}`)
	}, time.Time{})
	row2, err := mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 3 * time.Second, RunNonce: mNonce}, d2)
	wait2()
	if err == nil || row2.AnalyzeRC != 5 {
		t.Fatalf("replayed ANALYZE_FAILED rc=%d err=%v", row2.AnalyzeRC, err)
	}
}

func TestAnalyzedStaleRefused(t *testing.T) {
	// (a) an ANALYZED planted before READY is refused at READY time
	d := t.TempDir()
	os.WriteFile(filepath.Join(d, AnalyzedFile), mRow("", 0), 0o640)
	row, err := mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 200 * time.Millisecond}, d)
	if err == nil || row.AnalyzeRC != 5 {
		t.Fatalf("pre-planted ANALYZED accepted: err=%v rc=%d", err, row.AnalyzeRC)
	}
	// JW5A-05: the refusal must be the preexisting-marker check, not another rc 5 (stale mtime) masking a removed check
	if !strings.Contains(err.Error(), "marker_preexisting") {
		t.Fatalf("case (a) refused for the wrong reason: %v", err)
	}
	// (b) ANALYZED written after READY but with an mtime older than READY
	d = t.TempDir()
	wait := mHost(t, d, func(m map[string]any) (string, []byte) { return AnalyzedFile, mRow("", 0) }, time.Now().Add(-time.Hour))
	row, err = mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 3 * time.Second}, d)
	wait()
	if err == nil || row.AnalyzeRC != 5 {
		t.Fatalf("stale-mtime ANALYZED accepted: err=%v rc=%d", err, row.AnalyzeRC)
	}
	// (c) ANALYZED of another chunk
	d = t.TempDir()
	wait = mHost(t, d, func(m map[string]any) (string, []byte) { return AnalyzedFile, mRow("", 7) }, time.Time{})
	row, err = mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 3 * time.Second}, d)
	wait()
	if err == nil || row.AnalyzeRC != 5 {
		t.Fatalf("wrong-chunk ANALYZED accepted: err=%v rc=%d", err, row.AnalyzeRC)
	}
	// (d) ANALYZE_FAILED naming another chunk: rc 5 (not its claimed rc 3)
	d = t.TempDir()
	wait = mHost(t, d, func(m map[string]any) (string, []byte) {
		return AnalyzeFailedFile, []byte(`{"chunk":9,"rc":3,"reason":"x"}`)
	}, time.Time{})
	row, err = mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 3 * time.Second}, d)
	wait()
	if err == nil || row.AnalyzeRC != 5 {
		t.Fatalf("wrong-chunk ANALYZE_FAILED rc=%d err=%v", row.AnalyzeRC, err)
	}
	// (e) control: a fresh matching ANALYZED is accepted
	d = t.TempDir()
	wait = mHost(t, d, func(m map[string]any) (string, []byte) { return AnalyzedFile, mRow("", 0) }, time.Time{})
	row, err = mAnalyze(t, &HostAnalyzer{W: tw, H: th, Wait: 3 * time.Second}, d)
	wait()
	if err != nil || row.AnalyzeRC != 0 {
		t.Fatalf("fresh ANALYZED refused: err=%v rc=%d", err, row.AnalyzeRC)
	}
}

func jsonUnmarshal(b []byte, v any) error { return json.Unmarshal(b, v) }
