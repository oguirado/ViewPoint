package main

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

func TestRuntimeParentsPrivateProfile(t *testing.T) {
	root := t.TempDir()
	path := func(p string) string { return filepath.Join(root, p) }
	for _, p := range []string{"/run/user/10001", "/profile"} {
		if err := os.MkdirAll(path(p), 0700); err != nil {
			t.Fatal(err)
		}
	}
	var chmods []string
	fs := directoryOps{
		mkdirAll: func(p string, m os.FileMode) error { return os.MkdirAll(path(p), m) },
		mkdir:    func(p string, m os.FileMode) error { return os.Mkdir(path(p), m) },
		lstat:    func(p string) (os.FileInfo, error) { return os.Lstat(path(p)) },
		chmod:    func(p string, m os.FileMode) error { chmods = append(chmods, p); return os.Chmod(path(p), m) },
	}
	for _, p := range []string{"/run/vqa/fb", "/run/user/10001", "/profile", "/profile/cache"} {
		if err := makeGuestDirectory(p, 0700, fs); err != nil {
			t.Fatal(err)
		}
	}
	for _, p := range []string{"/run/vqa", "/run/user"} {
		s, err := os.Stat(path(p))
		if err != nil || s.Mode().Perm() != 0755 {
			t.Fatalf("parent %s: %v %v", p, s, err)
		}
	}
	for _, p := range []string{"/profile", "/profile/cache", "/run/vqa/fb", "/run/user/10001"} {
		s, err := os.Stat(path(p))
		if err != nil || s.Mode().Perm() != 0700 {
			t.Fatalf("leaf %s: %v %v", p, s, err)
		}
	}
	if strings.Join(chmods, ",") != "/run/user" {
		t.Fatalf("unexpected chmod: %v", chmods)
	}
	// Already traversable parents must not be chmodded again.
	if err := makeGuestDirectory("/run/user/10001", 0700, fs); err != nil {
		t.Fatal(err)
	}
	if len(chmods) != 1 {
		t.Fatal(chmods)
	}
	if err := os.Symlink(path("/profile"), path("/run/link")); err != nil {
		t.Fatal(err)
	}
	if err := makeGuestDirectory("/run/link/fb", 0700, fs); err == nil {
		t.Fatal("accepted symlink parent")
	}
}

func TestProgramExitReportsBeforeShutdown(t *testing.T) {
	for _, ready := range []bool{false, true} {
		t.Run(map[bool]string{false: "before-ready", true: "after-ready"}[ready], func(t *testing.T) {
			a, b := net.Pipe()
			defer b.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			ops := fakeBootOps()
			stopped := make(chan struct{}, 2)
			ops.start = func(s commandSpec) (childProcess, error) {
				done := make(chan error, 1)
				if s.Path == "/usr/local/bin/identity-entrypoint.sh" {
					go func() {
						if ready {
							io.WriteString(s.Stdout, "xpra layer up\nPROGRAM_READY {}\n")
						}
						if s.Stderr != nil {
							io.WriteString(s.Stderr, "old diagnostic\nREFUSED: display unavailable\n")
						}
						done <- errors.New("exit status 4")
					}()
				}
				return childProcess{done: done, stop: func() { stopped <- struct{}{} }}, nil
			}
			finished := make(chan error, 1)
			go func() {
				finished <- runGuest(ctx, a, map[string]string{controlPort: "c", agentPort: "a", viewPort: "v"}, ops)
			}()
			io.WriteString(b, bootJSON(t, "1", `[{"id":"w1","x":0,"y":0,"w":100,"h":100}]`)+"\n")
			scanner := bufio.NewScanner(b)
			var lines []string
			for scanner.Scan() {
				lines = append(lines, scanner.Text())
			}
			if err := <-finished; err == nil {
				t.Fatal("program exit accepted")
			}
			if len(lines) == 0 || lines[len(lines)-1] != "SRF1 ERR REFUSED: display unavailable" {
				t.Fatalf("control: %q", lines)
			}
			if ready && len(lines) != 3 {
				t.Fatalf("ready relay lost: %q", lines)
			}
			if len(stopped) != 2 {
				t.Fatal("children not stopped")
			}
		})
	}
}

func TestProgramStderrBoundedAndRedacted(t *testing.T) {
	for _, input := range []string{"", "first\nlast without newline", "safe\npassword=fixture\n", "Authorization: fixture\n", strings.Repeat("é", 3000) + "\n", strings.Repeat("é", 180) + "\r\n"} {
		var tail programStderr
		for i := 0; i < len(input); i++ {
			tail.Write([]byte(input[i : i+1]))
		}
		got := tail.reason()
		if len("SRF1 ERR "+got) > 200 || strings.ContainsAny(got, "\r\n\x00") || !utf8.ValidString(got) {
			t.Fatalf("invalid reason %q", got)
		}
		if strings.Contains(got, "fixture") {
			t.Fatal("sensitive diagnostic leaked")
		}
		if input == "first\nlast without newline" && got != "last without newline" {
			t.Fatal(got)
		}
	}
}

func TestGuestErrorStalledPeerIsBounded(t *testing.T) {
	a, b := net.Pipe()
	defer b.Close()
	finished := make(chan struct{})
	relay := &lineRelay{w: a}
	go func() { relay.reportError(a, "program exited"); close(finished) }()
	select {
	case <-finished:
	case <-time.After(2 * time.Second):
		a.Close()
		<-finished
		t.Fatal("stalled error peer blocked shutdown")
	}
}

func TestRuntimeParentRepairFailureStopsLeaf(t *testing.T) {
	root := t.TempDir()
	path := func(p string) string { return filepath.Join(root, p) }
	if err := os.MkdirAll(path("/run/user"), 0700); err != nil {
		t.Fatal(err)
	}
	fs := directoryOps{
		mkdirAll: func(p string, m os.FileMode) error { return os.MkdirAll(path(p), m) },
		mkdir:    func(string, os.FileMode) error { t.Error("created leaf after failed repair"); return nil },
		lstat:    func(p string) (os.FileInfo, error) { return os.Lstat(path(p)) },
		chmod:    func(p string, m os.FileMode) error { return os.ErrPermission },
	}
	if !errors.Is(makeGuestDirectory("/run/user/10001", 0700, fs), os.ErrPermission) {
		t.Fatal("failed repair accepted")
	}
}
