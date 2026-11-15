package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestNoOperatorVerbs(t *testing.T) {
	for _, verb := range []string{"assign", "unassign", "approve", "deny"} {
		var out bytes.Buffer
		if rc := run([]string{verb, "capsule-1", "chatgpt"}, &out); rc != 2 {
			t.Fatalf("%s rc=%d", verb, rc)
		}
	}
}

func TestCLIQueriesAndRequests(t *testing.T) {
	d := t.TempDir()
	path := filepath.Join(t.TempDir(), "catalog.json")
	if e := os.WriteFile(path, []byte(`["chatgpt"]`), 0600); e != nil {
		t.Fatal(e)
	}
	for _, tc := range []struct {
		args []string
		rc   int
	}{
		{[]string{"where", "chatgpt"}, 0}, {[]string{"status", "capsule-1"}, 0},
		{[]string{"request", "ai-a", "capsule-1", "chatgpt", "research"}, 0}, {[]string{"pending"}, 0},
		{[]string{"request", "ai-a", "capsule-1", "unknown", "research"}, 4},
		{[]string{"status", "../invalid"}, 2}, {[]string{"assign", "capsule-1", "chatgpt"}, 2},
		{[]string{"unassign", "capsule-1", "chatgpt"}, 2}, {[]string{"approve", "id"}, 2}, {[]string{"deny", "id"}, 2},
	} {
		var out bytes.Buffer
		args := append([]string{"-dir", d, "-catalog", path}, tc.args...)
		if rc := run(args, &out); rc != tc.rc {
			t.Fatalf("%v rc=%d %s", tc.args, rc, out.String())
		}
		if bytes.Count(out.Bytes(), []byte{'\n'}) != 1 || !json.Valid(bytes.TrimSpace(out.Bytes())) {
			t.Fatal(out.String())
		}
	}
}
func TestMalformedCatalog(t *testing.T) {
	for _, bad := range []string{`null`, `["ChatGPT"]`, `["chatgpt","chatgpt"]`, `["chatgpt"] {}`} {
		p := filepath.Join(t.TempDir(), "catalog.json")
		os.WriteFile(p, []byte(bad), 0600)
		var out bytes.Buffer
		if rc := run([]string{"-dir", t.TempDir(), "-catalog", p, "pending"}, &out); rc != 5 {
			t.Fatal(bad, rc)
		}
	}
}

func TestOversizeCatalogCannotHideTrailingJSON(t *testing.T) {
	p := filepath.Join(t.TempDir(), "catalog.json")
	data := append([]byte(`["chatgpt"]`), bytes.Repeat([]byte(" "), 1024*1024)...)
	data = append(data, []byte("true")...)
	if e := os.WriteFile(p, data, 0600); e != nil {
		t.Fatal(e)
	}
	var out bytes.Buffer
	if rc := run([]string{"-dir", t.TempDir(), "-catalog", p, "pending"}, &out); rc != 5 {
		t.Fatalf("oversize catalog accepted: rc=%d %s", rc, out.String())
	}
}
