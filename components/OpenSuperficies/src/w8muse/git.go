package w8muse

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/w8seat/patchpolicy"
	"opensuperficies.local/srf/src/workerbridge"
)

// CommandRunner is injectable; tests never invoke Git or a gate on the host.
type CommandRunner interface {
	Run(context.Context, []string, io.Reader, io.Writer) error
}
type ExecRunner struct{ Home string }

func (r ExecRunner) Run(ctx context.Context, argv []string, in io.Reader, out io.Writer) error {
	if len(argv) == 0 || argv[0] != "git" {
		return refusal(2, "input", "host command allowlist")
	}
	ctx, cancel := context.WithTimeout(ctx, 17*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin = in
	cmd.Stdout = out
	cmd.Stderr = io.Discard
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + r.Home, "LC_ALL=C", "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_TERMINAL_PROMPT=0", "GIT_ASKPASS=/bin/true", "GIT_LFS_SKIP_SMUDGE=1"}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.WaitDelay = 2 * time.Second
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return os.ErrProcessDone
		}
		e := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if errors.Is(e, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return e
	}
	if e := cmd.Run(); e != nil {
		return refusal(3, "setup", "git command failed")
	}
	return nil
}
func gitArgs(repo string, args ...string) []string {
	a := []string{"git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "-c", "credential.helper=", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=always", "-C", repo}
	return append(a, args...)
}

type cappedWriter struct {
	bytes.Buffer
	Limit    int
	exceeded bool
}

func (b *cappedWriter) Write(p []byte) (int, error) {
	if len(p) > b.Limit-b.Len() {
		b.exceeded = true
		return 0, refusal(5, "verification", "git output cap")
	}
	return b.Buffer.Write(p)
}
func capture(ctx context.Context, r CommandRunner, argv []string) ([]byte, error) {
	b := &cappedWriter{Limit: MaxHeader}
	e := r.Run(ctx, argv, nil, b)
	if b.exceeded {
		return nil, refusal(5, "verification", "git output cap")
	}
	return b.Bytes(), e
}

type countWriter struct{ n int64 }

func (c *countWriter) Write(b []byte) (int, error) {
	if int64(len(b)) > MaxBundle-c.n {
		return 0, refusal(2, "input", "bundle cap")
	}
	c.n += int64(len(b))
	return len(b), nil
}

// GitRepository does two passes: count into a sink, then stream into the seat.
// Neither pass creates a host bundle file. The mirror must already contain T.
// IntegrationRef is the mirror branch that is bundled and, in TipFromMirror
// mode, read as the observed tip. The conductor keeps the mirror as a
// `git fetch --prune origin '+refs/heads/*:refs/heads/*'` copy: a clone from a
// bundle only takes refs/heads/*, so a remote-tracking ref would arrive empty.
const IntegrationRef = "refs/heads/bridge/integration"

// GitRepository prepares the pinned bundle. TipFromMirror reads the observed
// integration tip from the mirror instead of an unauthenticated ls-remote.
type GitRepository struct {
	Runner        CommandRunner
	TipFromMirror bool
}

func (g GitRepository) Prepare(ctx context.Context, repo, pin, expectedRepo string) (Bundle, error) {
	var result Bundle
	if g.Runner == nil || !shaPattern.MatchString(pin) {
		return result, refusal(2, "input", "repository runner or pin")
	}
	remote, e := capture(ctx, g.Runner, gitArgs(repo, "remote", "get-url", "origin"))
	if e != nil {
		return result, e
	}
	url := strings.TrimSpace(string(remote))
	valid := url == "https://github.com/"+expectedRepo || url == "https://github.com/"+expectedRepo+".git" || url == "git@"+"github.com:"+expectedRepo+".git"
	if !valid {
		return result, refusal(2, "input", "mirror remote differs from dispatched repo")
	}
	var tip string
	if g.TipFromMirror {
		// Private repositories: the driver carries no credentials, so the observed tip is
		// the branch the conductor fetched into the read-only mirror.
		tipBytes, e := capture(ctx, g.Runner, gitArgs(repo, "rev-parse", "--verify", "--end-of-options", IntegrationRef+"^{commit}"))
		if e != nil {
			return result, refusal(4, "unavailable", "mirror integration tip unavailable")
		}
		tip = strings.TrimSpace(string(tipBytes))
		if !shaPattern.MatchString(tip) {
			return result, refusal(2, "input", "integration tip shape")
		}
	} else {
		// Avoid invoking a configured remote helper/ssh client from task data. Read
		// origin's URL for binding, but perform ls-remote using a closed HTTPS URL.
		tipBytes, e := capture(ctx, g.Runner, gitArgs(repo, "ls-remote", "--exit-code", "https://github.com/"+expectedRepo, "refs/heads/bridge/integration"))
		if e != nil {
			return result, refusal(4, "unavailable", "integration tip unavailable")
		}
		fields := strings.Fields(string(tipBytes))
		if len(fields) != 2 || fields[1] != "refs/heads/bridge/integration" || !shaPattern.MatchString(fields[0]) {
			return result, refusal(2, "input", "integration tip shape")
		}
		tip = fields[0]
	}
	if e = g.Runner.Run(ctx, gitArgs(repo, "merge-base", "--is-ancestor", pin, tip), nil, io.Discard); e != nil {
		return result, refusal(2, "input", "pin not proven reachable from observed integration tip")
	}
	// git refuses to bundle a bare object name, so bundle a named ref that contains
	// the pin; the runner checks out exactly the pinned commit from it.
	if !g.TipFromMirror {
		if e = g.Runner.Run(ctx, gitArgs(repo, "merge-base", "--is-ancestor", pin, IntegrationRef), nil, io.Discard); e != nil {
			return result, refusal(2, "input", "pin not reachable from the mirror's "+IntegrationRef)
		}
	}
	// Two passes (count, then stream) must emit identical bytes; threaded packing does
	// not (measured: three runs, three digests), so the bundle is packed single-threaded.
	argv := append(gitArgs(repo, "-c", "pack.threads=1"), "bundle", "create", "-", IntegrationRef)
	count := &countWriter{}
	if e = g.Runner.Run(ctx, argv, nil, count); e != nil {
		return result, e
	}
	if count.n == 0 {
		return result, refusal(2, "input", "empty bundle")
	}
	streamCtx, cancel := context.WithCancel(ctx)
	reader, writer := io.Pipe()
	done := make(chan struct{})
	go func() {
		defer close(done)
		e := g.Runner.Run(streamCtx, argv, nil, writer)
		_ = writer.CloseWithError(e)
	}()
	return Bundle{Tip: tip, Length: count.n, Reader: &bundleStream{PipeReader: reader, cancel: cancel, done: done}}, nil
}

type bundleStream struct {
	*io.PipeReader
	cancel context.CancelFunc
	done   chan struct{}
	once   sync.Once
}

func (s *bundleStream) Close() error {
	s.once.Do(func() { s.cancel(); _ = s.PipeReader.Close(); <-s.done })
	return nil
}

// GitPublisher is unreachable from --dry. It uses a disposable clone, stages
// only policy-checked delta files plus the two registered outbox paths, then
// pushes the exact auto_ingest branch. It never executes a task gate.
type GitPublisher struct{ Runner CommandRunner }

func (g GitPublisher) Publish(ctx context.Context, p Publication) error {
	if g.Runner == nil || p.Envelope.Mode != "live" || p.Envelope.Result["surface"] != workerbridge.OSSurface || p.Task["surface"] != workerbridge.OSSurface {
		return refusal(4, "unavailable", "live publication precondition")
	}
	result, e := workerbridge.OutboxBytes(p.Envelope)
	if e != nil {
		return e
	}
	task := text(p.Task["task_id"])
	attempt := text(p.Task["attempt_id"])
	branch := "bridge/" + task + "/" + attempt
	if !taskPattern.MatchString(task) || !attemptPattern.MatchString(attempt) || p.Task["writer_branch"] != branch || !shaPattern.MatchString(p.Commit) {
		return refusal(2, "input", "publication identity")
	}
	paths := writePaths(p.Task)
	patch := []byte(p.Artifacts["delta.patch"])
	changed := []patchpolicy.ChangedFile{}
	if len(patch) > 0 {
		changed, e = patchpolicy.Check(patch, paths)
		if e != nil {
			return refusal(5, "verification", "delta patch policy")
		}
	}
	for _, key := range []string{"result_path", "report_path"} {
		path := text(p.Task[key])
		if !safeRelative(path) || !strings.HasSuffix(path, "/"+task+"/"+attempt+"/"+map[string]string{"result_path": "result.json", "report_path": "report.md"}[key]) {
			return refusal(5, "verification", "outbox binding path")
		}
	}
	if e = os.MkdirAll(filepath.Dir(p.ScratchDir), 0700); e != nil {
		return e
	}
	if _, e = os.Lstat(p.ScratchDir); !os.IsNotExist(e) {
		return refusal(2, "input", "publication scratch exists")
	}
	if e = g.Runner.Run(ctx, gitArgs(p.RepoDir, "clone", "--no-hardlinks", "--no-tags", "--no-checkout", "--", p.RepoDir, p.ScratchDir), nil, io.Discard); e != nil {
		return e
	}
	defer os.RemoveAll(p.ScratchDir)
	run := func(args []string, in io.Reader) error {
		return g.Runner.Run(ctx, gitArgs(p.ScratchDir, args...), in, io.Discard)
	}
	if e = run([]string{"checkout", "--detach", p.Commit}, nil); e != nil {
		return e
	}
	if len(patch) > 0 {
		if e = run([]string{"apply", "--check", "--whitespace=error", "-"}, bytes.NewReader(patch)); e != nil {
			return refusal(5, "verification", "delta apply check")
		}
		if e = run([]string{"apply", "--whitespace=error", "-"}, bytes.NewReader(patch)); e != nil {
			return refusal(5, "verification", "delta apply")
		}
	}
	for _, v := range []struct {
		key  string
		data []byte
	}{{"result_path", result}, {"report_path", p.Report}} {
		path := filepath.Join(p.ScratchDir, filepath.FromSlash(text(p.Task[v.key])))
		if e = noSymlinkParents(p.ScratchDir, filepath.Dir(path)); e != nil {
			return e
		}
		if e = os.MkdirAll(filepath.Dir(path), 0700); e != nil {
			return e
		}
		f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC|syscall.O_NOFOLLOW, 0644)
		if e != nil {
			return e
		}
		_, e = f.Write(v.data)
		ce := f.Close()
		if e != nil {
			return e
		}
		if ce != nil {
			return ce
		}
	}
	add := []string{"add", "--"}
	for _, ch := range changed {
		add = append(add, ch.Path)
	}
	add = append(add, text(p.Task["result_path"]), text(p.Task["report_path"]))
	if e = run(add, nil); e != nil {
		return e
	}
	if e = run([]string{"-c", "user.name=W8 Muse", "-c", "user.email=w8-muse@localhost", "-c", "commit.gpgSign=false", "commit", "--no-verify", "-m", "W8 result " + task + " " + attempt}, nil); e != nil {
		return e
	}
	// No force, no inferred remote branch, no shell. An existing divergent branch
	// is refused by Git instead of rewriting another worker's result.
	return run([]string{"push", "https://github.com/" + text(p.Task["repo"]), "HEAD:refs/heads/" + branch}, nil)
}
func safeRelative(path string) bool {
	if path == "" || filepath.IsAbs(path) || strings.ContainsAny(path, "\\\x00") {
		return false
	}
	for _, part := range strings.Split(path, "/") {
		if part == "" || part == "." || part == ".." || part == ".git" {
			return false
		}
	}
	return true
}
func noSymlinkParents(root, path string) error {
	rel, e := filepath.Rel(root, path)
	if e != nil || !safeRelative(rel) {
		return refusal(5, "verification", "outbox parent")
	}
	current := root
	for _, part := range strings.Split(rel, string(filepath.Separator)) {
		current = filepath.Join(current, part)
		st, e := os.Lstat(current)
		if os.IsNotExist(e) {
			continue
		}
		if e != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
			return refusal(5, "verification", "outbox symlink parent")
		}
	}
	return nil
}
