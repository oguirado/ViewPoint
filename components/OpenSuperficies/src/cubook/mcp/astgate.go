package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"os/exec"
	"strings"
	"time"
)

// ASTChecker is the in-capsule gate every live script must pass before it is
// dispatched (FX3 ruling): nil error = allowed, rc 5 gate error = refused.
type ASTChecker interface {
	Check(ctx context.Context, script, outDir string) error
}

// ASTRunner runs capsule/astcheck/astcheck.py on a script and returns its rc
// and stdout. Tests inject a fake; ExecRunner is the real one.
type ASTRunner interface {
	Run(ctx context.Context, script, outDir string) (rc int, stdout []byte, err error)
}

// ASTGate is the ASTChecker over a Runner. It runs only inside the capsule:
// outside it (or without a runner) the script tools stay unwired, rc 5.
type ASTGate struct {
	Runner    ASTRunner
	InCapsule func() bool
}

type astVerdict struct {
	Schema  string `json:"schema"`
	Verdict string `json:"verdict"`
	Code    string `json:"code"`
	Reason  string `json:"reason"`
}

func (g *ASTGate) Check(ctx context.Context, script, outDir string) error {
	if g == nil || g.Runner == nil || g.InCapsule == nil || !g.InCapsule() {
		return gate("script_exec_unwired")
	}
	rc, out, err := g.Runner.Run(ctx, script, outDir)
	if err != nil {
		return gate("script_denied: ast_unavailable")
	}
	var v astVerdict
	if json.Unmarshal(bytes.TrimSpace(out), &v) != nil {
		return gate("script_denied: ast_verdict_unreadable")
	}
	switch {
	case rc == 0 && v.Schema == "os.cu.ast/v1" && v.Verdict == "allow":
		return nil
	case rc == 5:
		return gate("script_denied: ast_denied: " + strings.ReplaceAll(v.Reason, "\n", " "))
	}
	return gate("script_denied: ast_error")
}

// ExecRunner runs the checker with the capsule's stock python3 in isolated mode.
type ExecRunner struct {
	Python  string // default python3
	Script  string // path of astcheck.py inside the capsule
	Timeout time.Duration
}

func (r ExecRunner) Run(ctx context.Context, script, outDir string) (int, []byte, error) {
	py, to := r.Python, r.Timeout
	if py == "" {
		py = "python3"
	}
	if to <= 0 {
		to = 10 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, to)
	defer cancel()
	args := []string{"-I", "-B", r.Script}
	if outDir != "" {
		args = append(args, "--out-dir", outDir)
	}
	cmd := exec.CommandContext(ctx, py, args...)
	cmd.Stdin = strings.NewReader(script)
	var out bytes.Buffer
	cmd.Stdout = &limitWriter{w: &out, n: 8192}
	err := cmd.Run()
	if ee, ok := err.(*exec.ExitError); ok && ctx.Err() == nil {
		return ee.ExitCode(), out.Bytes(), nil
	}
	if err != nil {
		return 0, nil, err
	}
	return 0, out.Bytes(), nil
}

type limitWriter struct {
	w *bytes.Buffer
	n int
}

func (l *limitWriter) Write(p []byte) (int, error) {
	if room := l.n - l.w.Len(); room > 0 {
		if len(p) < room {
			room = len(p)
		}
		l.w.Write(p[:room])
	}
	return len(p), nil
}
