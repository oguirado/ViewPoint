package main

import (
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/capture"
)

// JW4-05: NewRunNonce() == "" -> rc 5 E_GATE run_nonce before anything else runs (the HostAnalyzer must never get an empty fence).
func TestWlcapNonceFailRc5(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	old := capture.NewRunNonce
	capture.NewRunNonce = func() string { return "" }
	t.Cleanup(func() { capture.NewRunNonce = old })
	rc, _, e := runCLI("--fake-fb", "--ramws-dir", t.TempDir(), "--out", t.TempDir(), "--frames", "10", "--mode", "wl", "--det", "on", "--analyze", "host")
	if rc != 5 || !strings.Contains(e, "run_nonce") {
		t.Fatalf("rc=%d %s", rc, e)
	}
}
