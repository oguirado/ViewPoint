//go:build cockpit

package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestTelemetriaCapsulaReal(t *testing.T) {
	telem := nuevaTelemetria()
	telem.inicio = time.Now().Add(-time.Hour)
	epoch := time.Now()
	first := telem.Capsula(os.Getpid(), epoch)
	if first.CPU != nil || first.Memory == nil {
		t.Fatalf("first = %+v", first)
	}
	if first.Memory.Timestamp < 1 || first.Memory.Timestamp > max(int64(1), time.Since(epoch).Milliseconds()) || first.Memory.Value <= 0 {
		t.Fatalf("memory = %+v", first.Memory)
	}
	if first.Memory.Source != "procfs" && first.Memory.Source != "cgroup" {
		t.Fatal(first.Memory.Source)
	}
	clamped := nuevaTelemetria().Capsula(os.Getpid(), time.Now().Add(time.Minute))
	if clamped.Memory == nil || clamped.Memory.Timestamp != 1 {
		t.Fatalf("clamped: %+v", clamped.Memory)
	}
	time.Sleep(15 * time.Millisecond)
	next := telem.Capsula(os.Getpid(), epoch)
	if next.CPU == nil || next.CPU.Value < 0 || next.CPU.Timestamp < 1 {
		t.Fatalf("second = %+v", next)
	}
	for _, pid := range []int{0, -3, 2147483647} {
		got := telem.Capsula(pid, epoch)
		if got.CPU != nil || got.Memory != nil {
			t.Fatalf("pid %d: %+v", pid, got)
		}
	}
}

func TestTelemetriaCapsulaIndependentLock(t *testing.T) {
	telem := nuevaTelemetria()
	telem.mu.Lock()
	defer telem.mu.Unlock()
	done := make(chan StateTelemetry, 1)
	go func() { done <- telem.Capsula(os.Getpid(), time.Now()) }()
	select {
	case got := <-done:
		if got.Memory == nil {
			t.Fatal("missing RAM")
		}
	case <-time.After(50 * time.Millisecond):
		t.Fatal("Capsula waits for host lock")
	}
}

func TestTelemetriaCapsulaDeltasAndEviction(t *testing.T) {
	telem := nuevaTelemetria()
	sample := muestraCap{cpuNs: 100, wallNs: 1000, ts: 1, source: "procfs", identidad: "same"}
	got := telem.registrarCapsula(1, sample, 123)
	if got.CPU != nil || got.Memory == nil {
		t.Fatal(got)
	}
	sample.cpuNs += int64(runtime.NumCPU()+10) * 1_000_000
	sample.wallNs += 1_000_000
	sample.ts++
	got = telem.registrarCapsula(1, sample, 123)
	if got.CPU == nil || got.CPU.Value != int64(runtime.NumCPU()+10)*1000 {
		t.Fatalf("unclipped delta: %+v", got.CPU)
	}
	// Non-forward wall clock has no CPU delta; resets and source changes must not invent one either.
	if telem.registrarCapsula(1, sample, 123).CPU != nil {
		t.Fatal("zero dt")
	}
	sample.cpuNs = 0
	sample.wallNs++
	sample.ts++
	if telem.registrarCapsula(1, sample, 0).CPU != nil {
		t.Fatal("counter reset")
	}
	sample.source = "cgroup"
	sample.cpuNs++
	sample.wallNs++
	if telem.registrarCapsula(1, sample, 123).CPU != nil {
		t.Fatal("source changed")
	}
	for pid := 2; pid <= 65; pid++ {
		sample.ts = int64(pid + 10)
		telem.registrarCapsula(pid, sample, 123)
	}
	if len(telem.prevCap) != 64 {
		t.Fatalf("map size %d", len(telem.prevCap))
	}
	if _, ok := telem.prevCap[1]; ok {
		t.Fatal("oldest retained")
	}
}

func TestTelemetriaCapsulaSources(t *testing.T) {
	// Fixtures, never frames; every write stays on this lane's scratch surface.
	root := filepath.Join("..", "build", "room_BSE", "proc-fixtures")
	if err := os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	root, err := filepath.Abs(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("TMPDIR", root)
	dir := t.TempDir()
	proc, groups := filepath.Join(dir, "proc"), filepath.Join(dir, "cgroup")
	write := func(name, data string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	// field 3 onward: state, then fields through starttime. Spaces/parentheses in comm are legal.
	fields := make([]string, 20)
	for i := range fields {
		fields[i] = "0"
	}
	fields[0] = "R"
	fields[11] = "10"
	fields[12] = "20"
	fields[19] = "123"
	base := filepath.Join(proc, "9")
	stat := fmt.Sprintf("9 (name ) with spaces) %s", strings.Join(fields, " "))
	write(filepath.Join(base, "stat"), stat)
	write(filepath.Join(base, "statm"), "100 5 0")
	write(filepath.Join(base, "cgroup"), "0::/slot9\n")
	write(filepath.Join(proc, "self", "cgroup"), "0::/cockpit\n")
	write(filepath.Join(groups, "slot9", "cpu.stat"), "usage_usec 123456\nuser_usec 123\n")
	write(filepath.Join(groups, "slot9", "memory.current"), "104857600\n")
	cpu, ram, source, _, ok := leerCapsula(9, proc, groups)
	if !ok || source != "cgroup" || cpu != 123456000 || ram != 104857600 {
		t.Fatalf("cgroup: %d %d %s %v", cpu, ram, source, ok)
	}
	// Sharing a cgroup means its numbers cannot be attributed to the capsule.
	write(filepath.Join(proc, "self", "cgroup"), "0::/slot9\n")
	cpu, ram, source, _, ok = leerCapsula(9, proc, groups)
	if !ok || source != "procfs" || cpu != 300000000 || ram != 5*int64(os.Getpagesize()) {
		t.Fatalf("procfs: %d %d %s %v", cpu, ram, source, ok)
	}
	// An incomplete distinct cgroup falls back to procfs, not partial or fabricated values.
	write(filepath.Join(proc, "self", "cgroup"), "0::/cockpit\n")
	write(filepath.Join(groups, "slot9", "memory.current"), "not a gauge")
	_, _, source, _, ok = leerCapsula(9, proc, groups)
	if !ok || source != "procfs" {
		t.Fatal("partial cgroup did not fall back")
	}
	write(filepath.Join(base, "statm"), "unreadable sample")
	if _, _, _, _, ok := leerCapsula(9, proc, groups); ok {
		t.Fatal("malformed sample accepted")
	}
	fields[0] = "Z"
	write(filepath.Join(base, "stat"), fmt.Sprintf("9 (gone) %s", strings.Join(fields, " ")))
	if _, _, _, _, ok := leerCapsula(9, proc, groups); ok {
		t.Fatal("exited pid accepted")
	}
}
