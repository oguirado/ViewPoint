package ajson

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"opensuperficies.local/srf/src/cubook/rc"
)

func writeSteps(t *testing.T, p string, ns ...uint32) {
	t.Helper()
	w, err := Open(p)
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range ns {
		if err := w.Append(mk(n)); err != nil {
			t.Fatal(err)
		}
	}
	w.Close()
}

func TestAJSONRefusesDuplicateStepAndMidFileCorrupt(t *testing.T) {
	dir := t.TempDir()
	// duplicate / non-increasing step on Append
	p := filepath.Join(dir, "a.jsonl")
	w, _ := Open(p)
	if err := w.Append(mk(1)); err != nil {
		t.Fatal(err)
	}
	for _, n := range []uint32{1, 0} {
		if err := w.Append(mk(n)); err == nil || rc.Of(err) != 2 {
			t.Fatalf("step %d after 1 must be E_SCHEMA rc 2, got %v", n, err)
		}
	}
	w.Close()
	// reopen: a second run of the same plan must not append step:1 again
	w, err := Open(p)
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Append(mk(1)); err == nil {
		t.Fatal("rerun duplicate step:1 accepted")
	}
	w.Close()
	if got, _ := ReadAll(p); len(got) != 1 {
		t.Fatalf("log polluted: %d lines", len(got))
	}
	// duplicate lines already in a file -> Open and ReadAll refuse
	b, _ := os.ReadFile(p)
	d := filepath.Join(dir, "dup.jsonl")
	os.WriteFile(d, append(append([]byte{}, b...), b...), 0o600)
	if _, err := ReadAll(d); rc.Of(err) != 2 {
		t.Fatalf("ReadAll duplicate: %v", err)
	}
	if w, err := Open(d); err == nil || rc.Of(err) != 2 {
		if w != nil {
			w.Close()
		}
		t.Fatalf("Open duplicate: %v", err)
	}
	// mid-file corrupt line: refuse, never truncate the valid lines after it
	c := filepath.Join(dir, "mid.jsonl")
	writeSteps(t, c, 1, 2, 3, 4, 5)
	raw, _ := os.ReadFile(c)
	lines := bytes.Split(bytes.TrimRight(raw, "\n"), []byte("\n"))
	lines[1] = []byte(`{"schema":"vqa.act-step/v1","bogus`)
	mod := append(bytes.Join(lines, []byte("\n")), '\n')
	os.WriteFile(c, mod, 0o600)
	if w, err := Open(c); err == nil || rc.Of(err) != 2 {
		if w != nil {
			w.Close()
		}
		t.Fatalf("mid-file corruption must be E_SCHEMA rc 2, got %v", err)
	}
	after, _ := os.ReadFile(c)
	if !bytes.Equal(after, mod) {
		t.Fatalf("file modified: %d -> %d bytes", len(mod), len(after))
	}
	// a torn FINAL line is still repaired
	f, _ := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString(`{"schema":"vqa.act-st`)
	f.Close()
	w, err = Open(p)
	if err != nil || w.Recovered == 0 {
		t.Fatalf("torn tail not repaired: %v", err)
	}
	w.Close()
}

// Kills mutant M10 (Append without the fsync barrier).
func TestAJSONAppendSyncs(t *testing.T) {
	old := syncFile
	defer func() { syncFile = old }()
	n := 0
	syncFile = func(f *os.File) error { n++; return f.Sync() }
	w, _ := Open(filepath.Join(t.TempDir(), "s.jsonl"))
	defer w.Close()
	for i := uint32(1); i <= 3; i++ {
		w.Append(mk(i))
	}
	if n != 3 {
		t.Fatalf("fsync called %d times for 3 appends", n)
	}
}
