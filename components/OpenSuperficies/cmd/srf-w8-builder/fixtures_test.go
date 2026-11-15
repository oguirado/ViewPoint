package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"opensuperficies.local/srf/src/w8builder"
)

func TestLeakFixturesAreRefused(t *testing.T) {
	root := filepath.Join("..", "..", "testdata", "w8builder", "leak")
	for _, name := range []string{"L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9"} {
		t.Run(name, func(t *testing.T) {
			data, err := os.ReadFile(filepath.Join(root, name+".Dockerfile"))
			if err != nil {
				t.Fatal(err)
			}
			if rc := w8builder.ExitCode(w8builder.LintDockerfile(data)); rc != 5 {
				t.Fatalf("leak rc=%d", rc)
			}
		})
	}
}
func TestFakebinVersionSelectionAndIsolation(t *testing.T) {
	root := t.TempDir()
	state := t.TempDir()
	fake, err := filepath.Abs(filepath.Join("..", "..", "testdata", "w8builder", "fakebin"))
	if err != nil {
		t.Fatal(err)
	}
	call := func(name string, args []string, env ...string) (string, int) {
		t.Helper()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, filepath.Join(fake, name), args...)
		cmd.Env = append(os.Environ(), "OSW_ROOT="+root, "OSW_FAKE_STATE="+state)
		cmd.Env = append(cmd.Env, env...)
		out, err := cmd.CombinedOutput()
		code := 0
		if err != nil {
			var e *exec.ExitError
			if !errors.As(err, &e) {
				t.Fatal(err)
			}
			code = e.ExitCode()
		}
		return string(out), code
	}
	installed, rc := call("dpkg-query", []string{"-W", "-f=${Version}", "docker-ce"})
	if rc != 0 {
		t.Fatalf("dpkg %d: %s", rc, installed)
	}
	available, rc := call("apt-cache", []string{"madison", "docker-ce-rootless-extras"})
	if rc != 0 || !strings.Contains(available, strings.TrimSpace(installed)) {
		t.Fatalf("madison %d %s", rc, available)
	}
	available, rc = call("apt-cache", []string{"madison", "docker-ce-rootless-extras"}, "OSW_FAKE_AVAILABLE_VERSION=unavailable")
	if rc != 0 || strings.Contains(available, strings.TrimSpace(installed)) {
		t.Fatalf("mismatch %d %s", rc, available)
	}
	if _, rc = call("apt-get", []string{"install", "wrong-package"}); rc != 2 {
		t.Fatalf("unexpected package rc %d", rc)
	}
	if _, rc = call("install", []string{"-d", "/outside-scratch"}); rc != 5 {
		t.Fatalf("outside root rc %d", rc)
	}
	if _, rc = call("systemd-run", []string{"--machine=osworker@", "--user", "--wait", "--pipe", "--quiet", "unknown-host-command"}); rc != 2 {
		t.Fatalf("unknown host command rc %d", rc)
	}
	if _, rc = call("id", []string{"-u"}, "OSW_FAKE_FAIL=id", "OSW_FAKE_RC=1"); rc != 5 {
		t.Fatalf("exit1 normalized %d", rc)
	}
	if _, rc = call("id", []string{"-u"}, "OSW_FAKE_STATE="); rc != 2 {
		t.Fatalf("missing state rc %d", rc)
	}
	// Native child status is fixture input, distinct from the application's rc.
	if _, rc = call("pgrep", []string{"-u", "osworker"}); rc != 1 {
		t.Fatalf("native no-match status %d", rc)
	}
	if _, rc = call("pgrep", []string{"-u", "osworker"}, "OSW_FAKE_FAIL=pgrep", "OSW_FAKE_RC=2"); rc != 2 {
		t.Fatalf("native probe error status %d", rc)
	}
}
func TestMainProcessEntry(t *testing.T) {
	if os.Getenv("W8_CLI_PROCESS") == "yes" {
		for i, arg := range os.Args {
			if arg == "--" {
				os.Args = append([]string{os.Args[0]}, os.Args[i+1:]...)
				main()
				return
			}
		}
		os.Exit(6)
	}
	for _, args := range [][]string{nil, {"unknown"}, {"lint", "--unknown-flag"}, {"approval-cmd", "--approval-id", "bad"}, {"build"}} {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		argv := append([]string{"-test.run=^TestMainProcessEntry$", "--"}, args...)
		cmd := exec.CommandContext(ctx, os.Args[0], argv...)
		cmd.Env = append(os.Environ(), "W8_CLI_PROCESS=yes")
		err := cmd.Run()
		cancel()
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.ExitCode() != 2 {
			t.Fatalf("args=%v error=%v", args, err)
		}
	}
}
func TestUnapprovedFixtureDigestIsNotAProductionPin(t *testing.T) {
	b, err := os.ReadFile(filepath.Join("..", "..", "testdata", "w8builder", "unapproved.json"))
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if err = json.Unmarshal(b, &value); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("W8 unapproved negative recipe fixture\n"))
	if value["recipe_sha256"] != hex.EncodeToString(sum[:]) || value["approval_id"] != "b2-0000" {
		t.Fatal("negative fixture identity changed")
	}
}
