package main

import (
	"bytes"
	"os"
	"path/filepath"
	"regexp"
	"testing"
)

func TestNoRc1(t *testing.T) {
	src, _ := filepath.Glob("*.go")
	re := regexp.MustCompile(`os\.Exit\(\s*1\s*\)|return\s+1\b`)
	for _, f := range src {
		if f == "main_test.go" {
			continue
		}
		b, _ := os.ReadFile(f)
		if re.Match(b) {
			t.Errorf("%s contains rc 1", f)
		}
	}
	cases := [][]string{
		nil, {"bogus"}, {"validate"}, {"validate", "--plan", "/nonexistent"}, {"validate", "--nope"},
		{"replay", "--plan", "/nonexistent"}, {"run", "--plan", "../../testdata/cu/plans/click_effect.plan.json"},
		{"validate", "--plan", "../../testdata/cu/plans/bad_float.plan.json"},
	}
	for _, c := range cases {
		var o, e bytes.Buffer
		if r := run(c, &o, &e); r == 1 || r < 0 || r > 7 {
			t.Errorf("%v -> rc %d", c, r)
		}
	}
	var o, e bytes.Buffer
	if r := run([]string{"validate", "--plan", "../../testdata/cu/plans/bad_float.plan.json"}, &o, &e); r != 2 {
		t.Errorf("bad plan rc %d", r)
	}
}

func TestRunReplayCLI(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	dir := t.TempDir()
	steps := filepath.Join(dir, "act_steps.jsonl")
	var o, e bytes.Buffer
	r := run([]string{"run", "--plan", "../../testdata/cu/plans/click_effect.plan.json", "--frames", "../../testdata/cu/plans/effect.frames.jsonl", "--out", steps, "--mock"}, &o, &e)
	if r != 0 {
		t.Fatalf("run rc %d: %s", r, e.String())
	}
	var outs [3][]byte
	for i := range outs {
		o.Reset()
		if r := run([]string{"replay", "--plan", "../../testdata/cu/plans/click_effect.plan.json", "--steps", steps, "--frames", "../../testdata/cu/plans/effect.frames.jsonl"}, &o, &e); r != 0 {
			t.Fatalf("replay rc %d %s", r, e.String())
		}
		outs[i] = append([]byte{}, o.Bytes()...)
	}
	rec, _ := os.ReadFile(steps)
	if !bytes.Equal(outs[0], outs[1]) || !bytes.Equal(outs[1], outs[2]) || !bytes.Equal(outs[0], rec) {
		t.Fatal("replay output differs")
	}
}
