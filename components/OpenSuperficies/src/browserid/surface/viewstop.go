package surface

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/sys/unix"
)

// An operator view in login mode (with or without --record) registers its pid so that
// `view-stop` can end it cleanly when Ctrl+C does not reach the terminal: the view then writes
// its stop row and final frames and restores readonly exactly as on Ctrl+C.

func (t Tool) viewPidPath(c string) string { return filepath.Join(t.root(), "views", c+".pid") }

// registerView records this process as the capsule's operator view; the returned func removes
// the record if it still names this process.
func (t Tool) registerView(c string) (func(), error) {
	dir := filepath.Dir(t.viewPidPath(c))
	if e := privateDir(dir); e != nil {
		return nil, e
	}
	me := strconv.Itoa(os.Getpid()) + "\n"
	if e := os.WriteFile(t.viewPidPath(c), []byte(me), 0600); e != nil {
		return nil, fail(4, "view registration unavailable")
	}
	return func() {
		if b, e := os.ReadFile(t.viewPidPath(c)); e == nil && string(b) == me {
			_ = os.Remove(t.viewPidPath(c))
		}
	}, nil
}

// isOperatorView checks that pid is this same CLI binary running `view --container c`.
func isOperatorView(pid int, c string) bool {
	self, e1 := os.Executable()
	exe, e2 := os.Readlink("/proc/" + strconv.Itoa(pid) + "/exe")
	raw, e3 := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/cmdline")
	if e1 != nil || e2 != nil || e3 != nil {
		return false
	}
	if real, e := filepath.EvalSymlinks(self); e == nil {
		self = real
	}
	if exe != self {
		return false
	}
	args := strings.Split(string(bytes.TrimRight(raw, "\x00")), "\x00")
	view, container := false, false
	for i, a := range args {
		switch {
		case a == "view" || a == "login":
			view = true
		case a == "--container" && i+1 < len(args) && args[i+1] == c, a == "--container="+c:
			container = true
		}
	}
	return view && container
}

// ViewStop ends the capsule's operator view as Ctrl+C would and waits for it to finish.
func (t Tool) ViewStop(ctx context.Context, c string) (map[string]any, error) {
	if !capsuleRE.MatchString(c) {
		return nil, fail(2, "invalid identity capsule")
	}
	b, e := os.ReadFile(t.viewPidPath(c))
	if e != nil || len(b) > 16 {
		return nil, fail(4, "no operator view registered for this capsule")
	}
	pid, e := strconv.Atoi(strings.TrimSpace(string(b)))
	if e != nil || pid < 2 || pid == os.Getpid() {
		return nil, fail(6, "invalid view registration")
	}
	if !isOperatorView(pid, c) {
		_ = os.Remove(t.viewPidPath(c)) // stale: that pid is not this capsule's view any more
		return nil, fail(4, "no operator view running for this capsule")
	}
	if e = unix.Kill(pid, unix.SIGINT); e != nil {
		return nil, fail(6, "view did not accept the stop signal")
	}
	start := time.Now()
	for time.Since(start) < 60*time.Second {
		if unix.Kill(pid, 0) != nil {
			return map[string]any{"stopped_view": c, "pid": pid, "seconds": time.Since(start).Round(100 * time.Millisecond).Seconds()}, nil
		}
		select {
		case <-ctx.Done():
			return nil, fail(6, "stop wait cancelled; the view is still closing")
		case <-time.After(200 * time.Millisecond):
		}
	}
	return nil, fail(6, "view still closing after 60 s")
}

// OperatorPasteMax bounds operator paste-text (UTF-8 bytes; the helper reads at most 16 KiB).
const OperatorPasteMax = 16384

// OperatorPaste delivers text into the capsule's focused window through the capsule clipboard
// while the operator's login view is open (the AI is locked out then, so this is the operator's
// own path; it refuses without a running operator view). The text is never logged; a recording
// sees it as one ctrl+v key row.
func (t Tool) OperatorPaste(ctx context.Context, c string, text []byte) (map[string]any, error) {
	if !capsuleRE.MatchString(c) {
		return nil, fail(2, "invalid identity capsule")
	}
	if len(text) == 0 || len(text) > OperatorPasteMax || !utf8.Valid(text) {
		return nil, fail(2, "text must be 1..16384 bytes of UTF-8")
	}
	b, e := os.ReadFile(t.viewPidPath(c))
	pid, e2 := strconv.Atoi(strings.TrimSpace(string(b)))
	if e != nil || e2 != nil || !isOperatorView(pid, c) {
		return nil, fail(4, "paste-text needs the operator's login view open on this capsule")
	}
	a := []string{"docker", "exec", "-i", "-u", "10001:10001", "-e", "DISPLAY=" + Display, "-e", "NO_AT_BRIDGE=1", c, "timeout", "--signal=TERM", "--kill-after=1s", "12s", "python3", "-I", "-c", pastePython}
	if _, e := t.R.Run(ctx, a, text); e != nil {
		return nil, fail(6, "paste failed")
	}
	return map[string]any{"pasted_runes": utf8.RuneCount(text), "container": c}, nil
}
