package w8builder

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestShellScriptsNeverExitOne(t *testing.T) {
	files, e := filepath.Glob("../../scripts/w8/*.sh")
	if e != nil || len(files) == 0 {
		t.Fatal("scripts absent")
	}
	for _, p := range files {
		b, e := os.ReadFile(p)
		if e != nil {
			t.Fatal(e)
		}
		lines := bytes.SplitN(b, []byte("\n"), 3)
		if len(lines) < 3 || !bytes.Equal(lines[1], []byte(`trap 'rc=$?; [ "$rc" -eq 1 ] && exit 5; exit "$rc"' EXIT`)) {
			t.Fatalf("trap missing: %s", p)
		}
		if out, e := exec.Command("/bin/sh", "-n", p).CombinedOutput(); e != nil {
			t.Fatalf("syntax: %s %s", p, out)
		}
		probe := append(bytes.Join(lines[:2], []byte("\n")), []byte("\nset -eu\nfalse\n")...)
		cmd := exec.Command("/bin/sh")
		cmd.Stdin = bytes.NewReader(probe)
		e = cmd.Run()
		if ex, ok := e.(*exec.ExitError); !ok || ex.ExitCode() != 5 {
			t.Fatalf("false did not normalize: %s %v", p, e)
		}
	}
	f := newShellFixture(t)
	for _, s := range []string{"osworker-setup.sh", "osworker-kill.sh", "osworker-verify.sh"} {
		if rc, out := f.run(t, s, "--bad-usage"); rc != 2 {
			t.Fatalf("usage %s rc%d %s", s, rc, out)
		}
	}
}
func TestWarmManifestAndAssertScriptCoverRepoGates(t *testing.T) {
	if e := Lint("../../.wavemind/env"); e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile("../../.wavemind/env/base/assert.sh")
	if e != nil {
		t.Fatal(e)
	}
	for _, name := range []string{"python3", "taskset", "go", "tini", "git", "jq", "ps", "godot", "blender", "rustc", "cmake"} {
		if !strings.Contains(string(b), name) {
			t.Fatalf("assert missing %s", name)
		}
	}
	for _, name := range []string{"opensuperficies", "vision-engine", "wender-agent", "godot", "blender"} {
		b, e := os.ReadFile("../../.wavemind/env/warm/" + name + ".json")
		if e != nil {
			t.Fatal(e)
		}
		if e = LintWarm(b); e != nil {
			t.Fatal(e)
		}
	}
}
