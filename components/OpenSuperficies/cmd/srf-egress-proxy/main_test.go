package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// --no-audit replaces --audit (never both); it reaches the listener without opening any audit file.
func TestNoAudit(t *testing.T) {
	dir := t.TempDir()
	registry := filepath.Join(dir, "registry.json")
	policy := filepath.Join(dir, "policy.json")
	if os.WriteFile(registry, []byte(`{"srf-id-a":"172.28.0.3"}`), 0600) != nil || os.WriteFile(policy, []byte(`{"schema":"browserid.proxy-policy/v1","approved":{"srf-id-a":["example.org"]},"denied":[]}`), 0600) != nil {
		t.Fatal("fixture")
	}
	// 192.0.2.1 (TEST-NET-1) is never a local address, so Listen fails with rc 4 after the audit stage.
	base := []string{"--registry", registry, "--policy", policy, "--listen", "192.0.2.1:3128"}
	for _, tc := range []struct {
		extra []string
		rc    int
	}{
		{nil, 2},
		{[]string{"--no-audit", "--audit", filepath.Join(dir, "a.jsonl")}, 2},
		{[]string{"--audit", filepath.Join(dir, "absent", "a.jsonl")}, 3},
		{[]string{"--no-audit=false"}, 2},
		{[]string{"--no-audit"}, 4},
		{[]string{"--audit", filepath.Join(dir, "a.jsonl")}, 4},
	} {
		var out bytes.Buffer
		if rc := run(append(append([]string(nil), base...), tc.extra...), &out); rc != tc.rc {
			t.Errorf("%v rc=%d want %d %s", tc.extra, rc, tc.rc, out.String())
		}
		if tc.rc == 4 && len(tc.extra) == 1 {
			if _, e := os.Lstat(filepath.Join(dir, "a.jsonl")); !os.IsNotExist(e) {
				t.Fatal("--no-audit opened an audit file")
			}
		}
	}
	if _, e := os.Lstat(filepath.Join(dir, "a.jsonl")); e != nil {
		t.Fatal("--audit did not open its file", e)
	}
}

func TestUsageAndFailClosed(t *testing.T) {
	for _, args := range [][]string{nil, {"--bogus"}, {"--registry", "missing", "--policy", "missing", "--audit", "missing"}} {
		var b bytes.Buffer
		rc := run(args, &b)
		if rc != 2 && rc != 3 {
			t.Fatal(rc)
		}
		var result map[string]any
		if json.Unmarshal(b.Bytes(), &result) != nil || result["code"] != float64(rc) {
			t.Fatal(b.String())
		}
	}
}

func TestExplicitSafeListenRequired(t *testing.T) {
	for _, addr := range []string{"", ":3128", "0.0.0.0:3128", "[::]:3128", "localhost:3128"} {
		args := []string{"--registry", "missing", "--policy", "missing", "--audit", "missing"}
		if addr != "" {
			args = append(args, "--listen", addr)
		}
		var out bytes.Buffer
		if rc := run(args, &out); rc != 2 {
			t.Errorf("listen %q rc=%d", addr, rc)
		}
	}
	for _, addr := range []string{"0.0.0.0:3128", "[::]:3128", "172.30.80.2:3128"} {
		args := []string{"--registry", "missing", "--policy", "missing", "--audit", "missing", "--listen", addr}
		if addr != "172.30.80.2:3128" {
			args = append(args, "--allow-any-listen")
		}
		var out bytes.Buffer
		if rc := run(args, &out); rc != 3 {
			t.Errorf("explicit listen %q failed usage gate: %d", addr, rc)
		}
	}
}
