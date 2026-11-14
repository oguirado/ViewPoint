// Package w8builder implements the operator-plane W8 base recipe builder.
// No API in this package is registered with the cockpit or seat socket.
package w8builder

import (
	"context"
	"errors"
	"io"
	"opensuperficies.local/srf/src/workerbridge"
	"os"
	"os/exec"
	"time"
)

type Runner interface {
	Run(context.Context, []string, io.Reader, io.Writer) error
}

// ExecRunner uses argv vectors, a closed environment, a deadline and process groups.
// Stderr is discarded deliberately: external tool output can contain secrets.
type ExecRunner struct{}

func (ExecRunner) Run(ctx context.Context, argv []string, in io.Reader, out io.Writer) error {
	if len(argv) == 0 {
		return refusal(2, "usage", "empty argv")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin = in
	cmd.Stdout = out
	cmd.Stderr = io.Discard
	for _, key := range []string{"PATH", "HOME", "XDG_RUNTIME_DIR", "DOCKER_HOST"} {
		if v, ok := os.LookupEnv(key); ok {
			cmd.Env = append(cmd.Env, key+"="+v)
		}
	}
	cmd.Env = append(cmd.Env, "LC_ALL=C", "GOTOOLCHAIN=local")
	configureProcess(cmd)
	cmd.WaitDelay = 2 * time.Second
	return cmd.Run()
}
func refusal(code int, class, detail string) error {
	return &workerbridge.Reject{Code: code, Class: class, Detail: detail}
}
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var r *workerbridge.Reject
	if errors.As(err, &r) && r.Code >= 2 && r.Code <= 7 {
		return r.Code
	}
	return 6
}

type limitedOutput struct {
	data     []byte
	cap      int
	overflow bool
}

func (b *limitedOutput) Write(p []byte) (int, error) {
	if len(p) > b.cap-len(b.data) {
		b.overflow = true
		return 0, refusal(5, "verification", "command output cap")
	}
	b.data = append(b.data, p...)
	return len(p), nil
}
func capture(ctx context.Context, r Runner, argv ...string) ([]byte, error) {
	b := &limitedOutput{cap: 1 << 20}
	err := r.Run(ctx, argv, nil, b)
	if b.overflow {
		return nil, refusal(5, "verification", "command output cap")
	}
	return b.data, err
}

// asyncRun keeps a Runner panic inside the builder's normalized failure domain.
func asyncRun(ctx context.Context, r Runner, argv []string, in io.Reader, out io.Writer) (err error) {
	defer func() {
		if recover() != nil {
			err = refusal(6, "indeterminate", "runner panic; quarantine")
		}
	}()
	return r.Run(ctx, argv, in, out)
}
func indeterminate(err error) bool {
	var r *workerbridge.Reject
	return errors.As(err, &r) && r.Code == 6
}
