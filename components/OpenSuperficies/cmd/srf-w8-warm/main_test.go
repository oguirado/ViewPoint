package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestRequiredFlags(t *testing.T) {
	var out, diag bytes.Buffer
	if got := run(context.Background(), nil, &out, &diag); got != 2 {
		t.Fatalf("missing flags rc=%d", got)
	}
	if got := run(context.Background(), []string{"--unknown"}, &out, &diag); got != 2 {
		t.Fatalf("unknown flag rc=%d", got)
	}
}
func TestMissingRulesNeverLaunch(t *testing.T) {
	dir := t.TempDir()
	r := filepath.Join(dir, "r.json")
	recipe, e := os.ReadFile("../../testdata/w8warm/recipe.json")
	if e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(r, recipe, 0600); e != nil {
		t.Fatal(e)
	}
	var out, diag bytes.Buffer
	code := run(context.Background(), []string{"--recipe", r, "--ledger", filepath.Join(dir, "not-present"), "--rules", filepath.Join(dir, "missing"), "--out", filepath.Join(dir, "result"), "--approve"}, &out, &diag)
	if code != 4 {
		t.Fatalf("prerequisite rc=%d", code)
	}
	if _, e := os.Lstat(filepath.Join(dir, "result")); !os.IsNotExist(e) {
		t.Fatalf("unexpected manifest %v", e)
	}
}
