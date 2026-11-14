package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/workerbridge"
)

func stubServices() services {
	return services{
		lint:      func(string) error { return nil },
		recipeSHA: func(string) (string, error) { return strings.Repeat("a", 64), nil },
		build: func(context.Context, w8builder.BuildConfig) (map[string]any, error) {
			return map[string]any{"ok": true}, nil
		},
		checkRecord: func([]byte) error { return nil },
	}
}
func invoke(args []string, in string, svc services) (int, string, string) {
	var out, diag bytes.Buffer
	rc := runWithServices(context.Background(), args, strings.NewReader(in), &out, &diag, svc)
	return rc, out.String(), diag.String()
}
func TestMainNeverExitsOne(t *testing.T) {
	cases := [][]string{nil, {"unknown"}, {"--help"}, {"lint", "--bad"}, {"lint", "-h"}, {"recipe-sha", "a", "b"}, {"approval-cmd"}, {"build"}, {"build", "other"}, {"record-check", "a", "b"}}
	for _, args := range cases {
		t.Run(strings.Join(args, "_"), func(t *testing.T) {
			rc, _, _ := invoke(args, "", stubServices())
			if rc != 2 {
				t.Fatalf("rc=%d, want usage 2", rc)
			}
		})
	}
	for _, code := range []int{2, 3, 4, 5, 6, 7} {
		svc := stubServices()
		svc.lint = func(string) error { return &workerbridge.Reject{Code: code, Class: "test", Detail: "controlled"} }
		if rc, _, _ := invoke([]string{"lint"}, "", svc); rc != code {
			t.Fatalf("code %d became %d", code, rc)
		}
	}
	svc := stubServices()
	svc.lint = func(string) error { return errors.New("raw failure") }
	if rc, _, _ := invoke([]string{"lint"}, "", svc); rc == 1 || rc == 0 {
		t.Fatalf("unexpected rc %d", rc)
	}
	svc.lint = func(string) error { panic("controlled") }
	if rc, _, _ := invoke([]string{"lint"}, "", svc); rc != 6 {
		t.Fatalf("panic rc %d", rc)
	}
}
func TestApprovalCommandPrintsOnlyAndNormalizesInstallFailure(t *testing.T) {
	svc := stubServices()
	rc, out, diag := invoke([]string{"approval-cmd", "--recipe-dir", "unused", "--approval-id", "b2-0001", "--note", "W8-B2 first base build"}, "", svc)
	if rc != 0 {
		t.Fatalf("%d: %s", rc, diag)
	}
	lines := strings.Split(strings.TrimSuffix(out, "\n"), "\n")
	if len(lines) != 2 || lines[0] != "recipe_sha256="+strings.Repeat("a", 64) || !strings.HasPrefix(lines[1], "sh -c ") {
		t.Fatalf("bad helper %q", out)
	}
	scratch := t.TempDir()
	// Only an inert fake sudo is invoked: the printed install never reaches /etc.
	fake := filepath.Join(scratch, "sudo")
	if err := os.WriteFile(fake, []byte("#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$FAKE_ARGS\"\ncat >\"$FAKE_APPROVAL\"\nexit \"${FAKE_SUDO_RC:-0}\"\n"), 0700); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{"0", "1", "2", "7"} {
		cmd := exec.CommandContext(context.Background(), "sh", "-c", lines[1])
		cmd.Env = append(os.Environ(), "PATH="+scratch+":"+os.Getenv("PATH"), "FAKE_SUDO_RC="+raw,
			"FAKE_ARGS="+filepath.Join(scratch, "args"), "FAKE_APPROVAL="+filepath.Join(scratch, "approval"))
		err := cmd.Run()
		got := 0
		if err != nil {
			var e *exec.ExitError
			if !errors.As(err, &e) {
				t.Fatal(err)
			}
			got = e.ExitCode()
		}
		want := map[string]int{"0": 0, "1": 5, "2": 2, "7": 7}[raw]
		if got != want {
			t.Fatalf("sudo %s: rc=%d want=%d", raw, got, want)
		}
	}
	argv, err := os.ReadFile(filepath.Join(scratch, "args"))
	if err != nil || string(argv) != "install\n-D\n-m\n0644\n-o\nroot\n-g\nroot\n/dev/stdin\n/etc/opensuperficies/builder_approval.json\n" {
		t.Fatalf("install argv %q, %v", argv, err)
	}
	data, err := os.ReadFile(filepath.Join(scratch, "approval"))
	if err != nil {
		t.Fatal(err)
	}
	var approval map[string]string
	if err := json.Unmarshal(data, &approval); err != nil {
		t.Fatal(err)
	}
	if len(approval) != 7 || approval["mode"] != "per_build" || approval["recipe_sha256"] != strings.Repeat("a", 64) || approval["approval_id"] != "b2-0001" || approval["operator_note"] != "W8-B2 first base build" {
		t.Fatalf("approval payload %q", data)
	}
}
func TestApprovalCommandRefusesBadOrPlaceholderInput(t *testing.T) {
	for _, args := range [][]string{
		{"approval-cmd", "--approval-id", "b2-1", "--note", "ok"},
		{"approval-cmd", "--approval-id", "b2-0001", "--note", "bad;command"},
		{"approval-cmd", "--approval-id", "b2-0001", "--note", strings.Repeat("x", 201)},
	} {
		if rc, out, _ := invoke(args, "", stubServices()); rc != 2 || out != "" {
			t.Fatalf("%v: %d %q", args, rc, out)
		}
	}
	svc := stubServices()
	svc.recipeSHA = func(string) (string, error) { return "<64hex>", nil }
	if rc, out, _ := invoke([]string{"approval-cmd", "--approval-id", "b2-0001", "--note", "ok"}, "", svc); rc != 2 || out != "" {
		t.Fatalf("placeholder: %d %q", rc, out)
	}
	svc = stubServices()
	svc.lint = func(string) error { return &workerbridge.Reject{Code: 2, Class: "pins", Detail: "placeholder"} }
	if rc, out, _ := invoke([]string{"approval-cmd", "--approval-id", "b2-0001", "--note", "ok"}, "", svc); rc != 2 || out != "" {
		t.Fatalf("lint refused: %d %q", rc, out)
	}
}
func TestCommandsForwardOnlyValidatedInputs(t *testing.T) {
	svc := stubServices()
	svc.build = func(_ context.Context, cfg w8builder.BuildConfig) (map[string]any, error) {
		b, err := io.ReadAll(cfg.Context)
		if err != nil || string(b) != "tar fixture" || cfg.ApprovalPath != "approval" || cfg.StateDir != "state" {
			t.Fatalf("bad cfg %#v bytes=%q", cfg, b)
		}
		return map[string]any{"ok": true}, nil
	}
	if rc, out, _ := invoke([]string{"build", "--approval-file", "approval", "--state-dir", "state", "-"}, "tar fixture", svc); rc != 0 || out != "{\"ok\":true}\n" {
		t.Fatalf("build %d %q", rc, out)
	}
	svc.checkRecord = func(b []byte) error {
		if string(b) != "{}" {
			t.Fatalf("record %q", b)
		}
		return nil
	}
	if rc, _, _ := invoke([]string{"record-check", "-"}, "{}", svc); rc != 0 {
		t.Fatal(rc)
	}
	if rc, _, _ := invoke([]string{"record-check", "-"}, strings.Repeat("x", 65537), svc); rc != 2 {
		t.Fatal(rc)
	}
}

func TestCLIForwardsExplicitRepositoryPins(t *testing.T) {
	pins := []w8builder.RepositoryPin{}
	for _, name := range []string{"opensuperficies", "vision-engine", "wender-agent"} {
		pins = append(pins, w8builder.RepositoryPin{Repo: name, Path: "/fixture/" + name, Commit: strings.Repeat("a", 40), Workspace: "/tasks/" + name})
	}
	data, _ := json.Marshal(pins)
	file := filepath.Join(t.TempDir(), "repo-pins.json")
	if e := os.WriteFile(file, data, 0600); e != nil {
		t.Fatal(e)
	}
	svc := stubServices()
	called := false
	svc.build = func(_ context.Context, cfg w8builder.BuildConfig) (map[string]any, error) {
		called = true
		if len(cfg.Repositories) != 3 || cfg.Repositories[0] != pins[0] {
			t.Fatal("repository pin data lost")
		}
		return map[string]any{"ok": true}, nil
	}
	if rc, _, diag := invoke([]string{"build", "--repo-pins", file, "-"}, "fixture", svc); rc != 0 || !called {
		t.Fatal(rc, diag)
	}
	called = false
	if e := os.WriteFile(file, []byte(`[{"repo":"opensuperficies","commit":"main"}]`), 0600); e != nil {
		t.Fatal(e)
	}
	if rc, _, _ := invoke([]string{"build", "--repo-pins", file, "-"}, "fixture", svc); rc != 2 || called {
		t.Fatal("invalid pins reached builder", rc)
	}
}
