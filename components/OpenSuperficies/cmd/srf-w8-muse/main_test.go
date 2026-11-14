package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestCLIExitCodesAndBoundedInput(t *testing.T) {
	for _, tc := range []struct {
		args  []string
		input string
		want  int
	}{{nil, "[]", 2}, {[]string{"--help"}, "", 0}, {[]string{"--unknown"}, "", 2}, {[]string{"--dry", "--campaign-dir", "unused", "--bridge-dir", "unused", "--repo-dir", "unused"}, `[{"task_id":"x","attempt":"a1","objective":"x","argv":["sh"]}]`, 2}, {[]string{"--dry", "--campaign-dir", "unused", "--bridge-dir", "unused", "--repo-dir", "unused"}, `[]`, 2}, {[]string{"--dry", "--tip-source", "cloud", "--campaign-dir", "unused", "--bridge-dir", "unused", "--repo-dir", "unused"}, `[{"task_id":"os-f-x-task","attempt":"a1","objective":"x"}]`, 2}} {
		var out, err bytes.Buffer
		if rc := run(tc.args, strings.NewReader(tc.input), &out, &err); rc != tc.want || rc == 1 {
			t.Fatalf("%v rc=%d err=%s", tc.args, rc, err.String())
		}
	}
}

func TestCLINamesPerItemRefusalOnStderr(t *testing.T) {
	var out, err bytes.Buffer
	args := []string{"--dry", "--campaign-dir", t.TempDir(), "--bridge-dir", t.TempDir(), "--repo-dir", t.TempDir()}
	if rc := run(args, strings.NewReader(`[{"task_id":"os-f-x-task","attempt":"a1","objective":"x"}]`), &out, &err); rc != 2 {
		t.Fatalf("rc=%d out=%s", rc, out.String())
	}
	if !strings.Contains(err.String(), "os-f-x-task a1:") || !strings.Contains(err.String(), "unregistered task") {
		t.Fatalf("stderr does not name the refusal: %q", err.String())
	}
}
