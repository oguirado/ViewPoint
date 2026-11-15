package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"opensuperficies.local/srf/src/cubook/capture"
)

const lawRoot = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"

func managedRun(t *testing.T, name string) string {
	t.Helper()
	if st, err := os.Stat("/dev/shm"); err != nil || !st.IsDir() {
		t.Skip("no /dev/shm")
	}
	if err := os.MkdirAll(lawRoot, 0o700); err != nil {
		t.Skip(err)
	}
	dir := filepath.Join(lawRoot, name)
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	os.WriteFile(filepath.Join(dir, "owner.json"), []byte("{}"), 0o600)
	lk, err := os.OpenFile(filepath.Join(dir, ".lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Flock(int(lk.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lk.Close() })
	return dir
}

func writeHostMarker(dir, name string, b []byte) error {
	tmp := filepath.Join(dir, name+".tmp")
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o640)
	if err != nil {
		return err
	}
	_, err = f.Write(b)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp, filepath.Join(dir, name))
	}
	if err != nil {
		os.Remove(tmp)
	}
	return err
}

// RD4-02: HostAnalyzer of srf-cu-wlcap receives the START run_nonce.
func TestWlcapHostAnalyzerGetsStartNonce(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	const fixedNonce = "0123456789abcdef0123456789abcdef"
	oldNonce := capture.NewRunNonce
	capture.NewRunNonce = func() string { return fixedNonce }
	t.Cleanup(func() { capture.NewRunNonce = oldNonce })

	ramDir := managedRun(t, "rd4-wlcap-nonce")
	outDir := t.TempDir()

	errCh := make(chan error, 1)
	go func() {
		defer close(errCh)
		deadline := time.Now().Add(5 * time.Second)

		// 1. Wait for START file and verify run_nonce
		startPath := filepath.Join(ramDir, capture.StartFile)
		var startMap map[string]any
		for {
			if time.Now().After(deadline) {
				errCh <- fmt.Errorf("timeout waiting for %s", capture.StartFile)
				return
			}
			b, err := os.ReadFile(startPath)
			if err == nil {
				if err := json.Unmarshal(b, &startMap); err == nil {
					break
				}
			}
			time.Sleep(2 * time.Millisecond)
		}

		nonceInStart, _ := startMap["run_nonce"].(string)
		if nonceInStart != fixedNonce {
			errCh <- fmt.Errorf("START run_nonce=%q, want %q", nonceInStart, fixedNonce)
			return
		}

		// 2. Wait for chunk_0 READY file and verify run_nonce
		chunk0Dir := filepath.Join(ramDir, "chunk_0")
		readyPath := filepath.Join(chunk0Dir, capture.ReadyFile)
		var readyMap map[string]any
		for {
			if time.Now().After(deadline) {
				errCh <- fmt.Errorf("timeout waiting for %s", readyPath)
				return
			}
			b, err := os.ReadFile(readyPath)
			if err == nil {
				if err := json.Unmarshal(b, &readyMap); err == nil {
					break
				}
			}
			time.Sleep(2 * time.Millisecond)
		}

		nonceInReady, ok := readyMap["run_nonce"].(string)
		if !ok || nonceInReady != fixedNonce {
			errCh <- fmt.Errorf("READY run_nonce=%q (ok=%v), want %q", readyMap["run_nonce"], ok, fixedNonce)
			return
		}

		// 3. Write ANALYZED marker with run_nonce
		ans := map[string]any{
			"chunk":                0,
			"frames":               10,
			"grid":                 16,
			"class_counts":         map[string]int64{"UNCHANGED": 10, "JITTER": 0, "CHANGED": 0, "MOVED": 0, "FLICKER": 0, "FROZEN": 0, "DRIFT": 0},
			"analyze_us_per_frame": 100,
			"engine_wheel_sha8":    "01234567",
			"run_nonce":            fixedNonce,
		}
		ansBytes, _ := json.Marshal(ans)
		if err := writeHostMarker(chunk0Dir, capture.AnalyzedFile, append(ansBytes, '\n')); err != nil {
			errCh <- fmt.Errorf("write ANALYZED marker: %w", err)
			return
		}
	}()

	rc, stdout, stderr := runCLI("--fake-fb", "--ramws-dir", ramDir, "--out", outDir, "--mode", "wl", "--det", "on", "--frames", "10", "--analyze", "host", "--analyze-wait-ms", "5000")
	if rc != 0 {
		t.Fatalf("runCLI rc=%d stdout=%q stderr=%q", rc, stdout, stderr)
	}

	if err := <-errCh; err != nil {
		t.Fatalf("host player error: %v", err)
	}
}
