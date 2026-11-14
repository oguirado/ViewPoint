package main

import (
	"context"
	"io"
	"os/exec"
	"sync"
	"syscall"
	"time"
)

type commands interface {
	run(context.Context, string, []string, io.Reader, io.Writer) (int, string, bool)
}
type execCommands struct{}

func (execCommands) run(ctx context.Context, dir string, argv []string, in io.Reader, out io.Writer) (int, string, bool) {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = dir
	cmd.Stdin = in
	cmd.Stdout = out
	cmd.Stderr = out
	cmd.Env = []string{"PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/home/worker", "TMPDIR=/tmp", "LC_ALL=C.UTF-8", "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_TERMINAL_PROMPT=0", "GIT_ASKPASS=/bin/true", "GIT_LFS_SKIP_SMUDGE=1", "GOTOOLCHAIN=local", "GOFLAGS=-mod=mod", "GOMODCACHE=/opt/cache/gomod", "GOCACHE=/workspace/.cache/go-build", "GOTMPDIR=/workspace/.gotmp", "CARGO_HOME=/opt/cache/cargo", "CARGO_NET_OFFLINE=true", "PIP_NO_INDEX=1"}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	cmd.WaitDelay = 250 * time.Millisecond
	e := cmd.Run()
	// A gate must not leave descendants alive after the direct process exits.
	if cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	if ctx.Err() != nil {
		return 124, "", true
	}
	if e == nil {
		return 0, "", false
	}
	if ee, ok := e.(*exec.ExitError); ok {
		if ws, ok := ee.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
			return 128 + int(ws.Signal()), ws.Signal().String(), false
		}
		return ee.ExitCode(), "", false
	}
	return 3, "", false
}

type limitedRing struct {
	mu    sync.Mutex
	data  []byte
	cap   int
	total int
}

func (r *limitedRing) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := len(p)
	r.total += n
	if n >= r.cap {
		r.data = append(r.data[:0], p[n-r.cap:]...)
	} else {
		excess := len(r.data) + n - r.cap
		if excess > 0 {
			copy(r.data, r.data[excess:])
			r.data = r.data[:len(r.data)-excess]
		}
		r.data = append(r.data, p...)
	}
	return n, nil
}
