package w8muse

import (
	"bytes"
	"context"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Real git: a bare commit name cannot be bundled ("Refusing to create empty
// bundle"); the fakes above could not catch that, the first live dry run did.
func TestMirrorModeBundlesPinnedCommitWithRealGit(t *testing.T) {
	if _, e := exec.LookPath("git"); e != nil {
		t.Skip("git absent")
	}
	dir := t.TempDir()
	work, mirror := filepath.Join(dir, "work"), filepath.Join(dir, "mirror.git")
	g := func(args ...string) string {
		cmd := exec.Command("git", args...)
		cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t", "GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t")
		out, e := cmd.CombinedOutput()
		if e != nil {
			t.Fatalf("git %v: %v %s", args, e, out)
		}
		return strings.TrimSpace(string(out))
	}
	g("init", "-q", "-b", "main", work)
	g("-C", work, "commit", "-q", "--allow-empty", "-m", "pin")
	pin := g("-C", work, "rev-parse", "HEAD")
	g("-C", work, "commit", "-q", "--allow-empty", "-m", "later")
	g("clone", "-q", "--bare", work, mirror)
	g("-C", mirror, "update-ref", IntegrationRef, "refs/heads/main")
	g("-C", mirror, "update-ref", "-d", "refs/heads/main")
	g("-C", mirror, "remote", "set-url", "origin", "https://github.com/team/project.git")
	b, e := (GitRepository{Runner: ExecRunner{Home: dir}, TipFromMirror: true}).Prepare(context.Background(), mirror, pin, "team/project")
	if e != nil {
		t.Fatal(e)
	}
	data, e := io.ReadAll(b.Reader)
	_ = b.Reader.Close()
	again, e2 := (GitRepository{Runner: ExecRunner{Home: dir}, TipFromMirror: true}).Prepare(context.Background(), mirror, pin, "team/project")
	if e2 != nil {
		t.Fatal(e2)
	}
	second, _ := io.ReadAll(again.Reader)
	_ = again.Reader.Close()
	if !bytes.Equal(data, second) || again.Length != b.Length {
		t.Fatal("bundle passes differ: the declared length would not match the stream")
	}
	if e != nil || int64(len(data)) != b.Length || b.Length == 0 {
		t.Fatalf("len %d/%d %v", len(data), b.Length, e)
	}
	bundle := filepath.Join(dir, "x.bundle")
	if e = os.WriteFile(bundle, data, 0o600); e != nil {
		t.Fatal(e)
	}
	clone := filepath.Join(dir, "clone")
	g("clone", "-q", "--no-checkout", bundle, clone)
	g("-C", clone, "checkout", "-q", "--detach", pin)
	if got := g("-C", clone, "rev-parse", "HEAD"); got != pin || !bytes.HasPrefix(data, []byte("# v2 git bundle")) && !bytes.HasPrefix(data, []byte("# v3 git bundle")) {
		t.Fatalf("head %s want %s", got, pin)
	}
}
