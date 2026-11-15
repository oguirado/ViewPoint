package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"syscall"
	"testing"
	"time"
)

func bootJSON(t *testing.T, windows string, tiles string) string {
	t.Helper()
	b, err := json.Marshal(map[string]string{"SRF_PROGRAM": "notepad-vm", "SRF_WINDOWS": windows, "SRF_TILES": tiles, "NO_AT_BRIDGE": "1"})
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestBootExactObjectsAndCatalogWindowLimit(t *testing.T) {
	tile := `{"id":"w1","x":0,"y":0,"w":100,"h":100}`
	good := bootJSON(t, "1", "["+tile+"]")
	for _, key := range []string{"SRF_PROGRAM", "SRF_WINDOWS", "SRF_TILES", "NO_AT_BRIDGE"} {
		var env map[string]string
		json.Unmarshal([]byte(good), &env)
		val, _ := json.Marshal(env[key])
		bad := strings.TrimSuffix(good, "}") + fmt.Sprintf(",%q:%s}", key, val)
		if _, err := parseBootEnv([]byte(bad)); err == nil {
			t.Errorf("accepted duplicate boot key %s", key)
		}
	}
	for _, key := range []string{"id", "x", "y", "w", "h"} {
		var fields map[string]json.RawMessage
		json.Unmarshal([]byte(tile), &fields)
		repeated := strings.TrimSuffix(tile, "}") + fmt.Sprintf(",%q:%s}", key, fields[key])
		if _, err := parseBootEnv([]byte(bootJSON(t, "1", "["+repeated+"]"))); err == nil {
			t.Errorf("accepted duplicate tile key %s", key)
		}
	}
	for _, badTile := range []string{
		`{"ID":"w1","x":0,"y":0,"w":100,"h":100}`,
		`{"id":"w1","x":null,"y":0,"w":100,"h":100}`,
		`{"id":"w1","x":0,"y":0,"w":100}`,
		`{"id":"w1","x":0,"y":0,"w":100,"h":100,"extra":0}`,
	} {
		if _, err := parseBootEnv([]byte(bootJSON(t, "1", "["+badTile+"]"))); err == nil {
			t.Errorf("accepted tile %s", badTile)
		}
	}
	for n := 1; n <= 5; n++ {
		var tiles []string
		for i := 0; i < n; i++ {
			tiles = append(tiles, fmt.Sprintf(`{"id":"w%d","x":%d,"y":0,"w":100,"h":100}`, i, i*100))
		}
		_, err := parseBootEnv([]byte(bootJSON(t, fmt.Sprint(n), "["+strings.Join(tiles, ",")+"]")))
		if (err == nil) != (n <= 4) {
			t.Errorf("window count %d: %v", n, err)
		}
	}
}

// The raw fake emulates a serial port: EAGAIN while O_NONBLOCK is set,
// then a blocking read while the parent sends the boot line later.
type serialFake struct {
	file *os.File
	fd   uintptr
}

func (f *serialFake) Fd() uintptr { return f.fd }
func (f *serialFake) Read(b []byte) (int, error) {
	n, err := syscall.Read(int(f.fd), b)
	if n < 0 {
		n = 0
	}
	return n, err
}
func (f *serialFake) Write(b []byte) (int, error) { return len(b), nil }
func (f *serialFake) Close() error                { return f.file.Close() }

func TestBootControlClearsNonblockAfterEAGAIN(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()
	f := &serialFake{file: r, fd: r.Fd()}
	if err = syscall.SetNonblock(int(f.fd), true); err != nil {
		t.Fatal(err)
	}
	if _, err = f.Read(make([]byte, 1)); !errors.Is(err, syscall.EAGAIN) {
		t.Fatalf("fake must first yield EAGAIN: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	ops := fakeBootOps()
	ops.start = func(commandSpec) (childProcess, error) {
		return childProcess{done: make(chan error), stop: func() {}}, nil
	}
	line := bootJSON(t, "1", `[{"id":"w1","x":0,"y":0,"w":100,"h":100}]`)
	sent := make(chan error, 1)
	go func() {
		timer := time.NewTimer(20 * time.Millisecond)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-ctx.Done():
			sent <- ctx.Err()
			return
		}
		_, err := io.WriteString(w, line+"\nSRF1 stop\n")
		w.Close()
		sent <- err
	}()
	if err = runGuest(ctx, f, map[string]string{controlPort: "c", agentPort: "a", viewPort: "v"}, ops); err != nil {
		t.Errorf("boot aborted before delayed data: %v", err)
	}
	if err = <-sent; err != nil {
		t.Fatal(err)
	}
}

func TestLegacyDFB68A6Golden(t *testing.T) {
	var trace strings.Builder
	reapAck := make(chan struct{})
	ops := legacyOps{
		mount: func(src, dst, kind string, flags uintptr, data string) {
			fmt.Fprintf(&trace, "mount %s %s %s %d %q\n", src, dst, kind, flags, data)
		},
		symlink:  func(src, dst string) { fmt.Fprintf(&trace, "symlink %s %s\n", src, dst) },
		mkdir:    func(path string, mode os.FileMode) { fmt.Fprintf(&trace, "mkdir %s %04o\n", path, mode) },
		stdout:   &trace,
		notify:   func(s string) { fmt.Fprintln(&trace, s) },
		reap:     func() { fmt.Fprintln(&trace, "reap"); reapAck <- struct{}{} },
		sync:     func() { fmt.Fprintln(&trace, "sync") },
		poweroff: func() { fmt.Fprintln(&trace, "reboot POWER_OFF") },
	}
	setupLegacyFilesystems(ops)
	announceLegacy(ops)
	signals := make(chan os.Signal)
	ticks := make(chan time.Time)
	done := make(chan struct{})
	go func() { runLegacy(signals, ticks, ops); close(done) }()
	// Acknowledge each reap to pin SIGCHLD then periodic reaping before shutdown.
	signals <- syscall.SIGCHLD
	<-reapAck
	ticks <- time.Time{}
	<-reapAck
	signals <- syscall.SIGHUP
	signals <- syscall.SIGTERM
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("legacy shutdown hung")
	}
	want := `mount proc /proc proc 0 ""
mount sysfs /sys sysfs 0 ""
mount devtmpfs /dev devtmpfs 0 ""
mount devpts /dev/pts devpts 0 "newinstance,ptmxmode=0666,mode=0620"
mount tmpfs /dev/shm tmpfs 0 "mode=1777"
mount tmpfs /run tmpfs 0 "mode=0755"
mount tmpfs /tmp tmpfs 0 "mode=1777"
symlink pts/ptmx /dev/ptmx
mkdir /run/newton-tenant 0770
mkdir /run/user/10001 0700
[srf-guest-init] MicroVM guest PID 1 initialized
SRF1 PID1
reap
reap
[srf-guest-init] received shutdown signal: terminated
SRF1 SHUTDOWN_BEGIN
sync
SRF1 POWER_OFF
reboot POWER_OFF
`
	if trace.String() != want {
		t.Fatalf("legacy trace drifted from dfb68a6\ngot:\n%swant:\n%s", trace.String(), want)
	}
}
