package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testArgs(dir, queue string) []string {
	return []string{
		"--queue", queue,
		"--bridge-dir", filepath.Join(dir, "bridge"),
		"--repository", "oguirado/OpenSuperficies",
		"--base-commit", strings.Repeat("a", 40),
		"--attempt", "a1",
		"--gate-argv-json", "[[\"go\",\"test\",\"./...\"]]",
		"--resource-policy-json", "{\"inference\":\"host\",\"max_cpu_ms\":1000,\"max_memory_bytes\":67108864,\"max_wall_ms\":1200}",
		"--obligations-json", "[\"preserve registered paths\"]",
		"--seat-ready",
		"--wave-out", filepath.Join(dir, "wave.json"),
		"--manifest-out", filepath.Join(dir, "manifest.json"),
	}
}

func TestInvalidQueueLeavesNoOutput(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "queue.txt")
	brief := filepath.Join(dir, "brief.md")
	if err := os.WriteFile(brief, []byte("task data\n"), 0600); err != nil {
		t.Fatal(err)
	}
	bad := "job-one\t" + brief + "\t" + filepath.Join(dir, "out.json") + "\tfourth\n"
	if err := os.WriteFile(path, []byte(bad), 0600); err != nil {
		t.Fatal(err)
	}
	var diagnostics bytes.Buffer
	if code := run(testArgs(dir, path), &diagnostics); code != 2 {
		t.Fatalf("invalid queue must exit 2, got %d, stderr=%s", code, diagnostics.String())
	}
	for _, name := range []string{"wave.json", "manifest.json"} {
		if _, err := os.Lstat(filepath.Join(dir, name)); !os.IsNotExist(err) {
			t.Fatalf("%s must not be created: %v", name, err)
		}
	}
}

func TestOpenWaveIsIndeterminate(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "queue.txt")
	brief := filepath.Join(dir, "brief.md")
	if err := os.WriteFile(brief, []byte("task data\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("job-one\t"+brief+"\t"+filepath.Join(dir, "out.json")+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	args := append(testArgs(dir, path), "--wave-open")
	var diagnostics bytes.Buffer
	if code := run(args, &diagnostics); code != 6 {
		t.Fatalf("open wave must exit EN_DUDA=6: got %d", code)
	}
	if _, err := os.Stat(filepath.Join(dir, "wave.json")); !os.IsNotExist(err) {
		t.Fatal("a second wave was published")
	}
}

func TestExclusivePublicationCannotOverwrite(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "wave.json")
	if err := exclusiveWrite(path, []byte("first")); err != nil {
		t.Fatal(err)
	}
	if err := exclusiveWrite(path, []byte("second")); err == nil {
		t.Fatal("overwrote an existing wave")
	}
	got, err := os.ReadFile(path)
	if err != nil || string(got) != "first" {
		t.Fatalf("existing wave was changed: %v %s", err, got)
	}
}

func TestInvalidCLIArguments(t *testing.T) {
	var diagnostics bytes.Buffer
	if code := run([]string{"--guest-inference"}, &diagnostics); code != 2 {
		t.Fatalf("missing prerequisites must exit 2, got %d", code)
	}
}
