package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestCommandProcess(t *testing.T) {
	if os.Getenv("SRF_AGENT_TEST") != "1" {
		return
	}
	mode := os.Args[len(os.Args)-1]
	switch mode {
	case "echo":
		io.Copy(os.Stdout, os.Stdin)
		os.Stderr.WriteString("tail")
		os.Exit(4)
	case "wait":
		time.Sleep(time.Minute)
	case "pair":
		// Model a command whose inherited stdout outlives its direct child.
		c := exec.Command(os.Args[0], "-test.run=^TestCommandProcess$", "--", "pair-tail")
		c.Stdout, c.Stderr, c.Env = os.Stdout, os.Stderr, os.Environ()
		if c.Start() != nil {
			os.Exit(3)
		}
	case "pair-tail":
		time.Sleep(700 * time.Millisecond)
		if _, err := os.Stdout.Write(pairBytes()); err != nil {
			os.Exit(3)
		}
		os.Stderr.WriteString("pair complete")
	case "flood":
		for i := 0; i < 100000; i++ {
			os.Stdout.Write(make([]byte, 65536))
		}
	case "slow-flood":
		for i := 0; i < 1000; i++ {
			os.Stdout.Write(make([]byte, 4096))
			time.Sleep(time.Millisecond)
		}
	case "oversize-header", "oversize-second-header", "pair-forever", "pair-trailing", "pair-truncated", "partial-header":
		var h [8]byte
		switch mode {
		case "oversize-header":
			binary.BigEndian.PutUint64(h[:], 20<<20)
			os.Stdout.Write(h[:])
			time.Sleep(time.Minute)
		case "oversize-second-header":
			os.Stdout.Write(h[:])
			binary.BigEndian.PutUint64(h[:], 20<<20)
			os.Stdout.Write(h[:])
			time.Sleep(time.Minute)
		case "partial-header":
			os.Stdout.Write(h[:3])
		case "pair-truncated":
			binary.BigEndian.PutUint64(h[:], 100)
			os.Stdout.Write(h[:])
		case "pair-trailing":
			os.Stdout.Write(make([]byte, 17))
		case "pair-forever":
			os.Stdout.Write(pairBytes())
			for i := 0; i < 100000; i++ {
				os.Stdout.Write(make([]byte, 65536))
			}
		}
	}
	os.Exit(0)
}
func fakeCommand(mode string) commandFunc {
	return func(ctx context.Context, _ []string) *exec.Cmd {
		c := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestCommandProcess$", "--", mode)
		return c
	}
}
func start(t *testing.T, mode string) (net.Conn, <-chan error) {
	t.Helper()
	a, b := net.Pipe()
	done := make(chan error, 1)
	// Race-instrumented subprocesses must close stdout on exit immediately;
	// the runtime's default exit sleep would hide EOF behind short test deadlines.
	go func() {
		done <- serve(context.Background(), a, fakeCommand(mode), []string{"SRF_AGENT_TEST=1", "GORACE=atexit_sleep_ms=0"})
	}()
	t.Cleanup(func() {
		b.Close()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("server did not stop")
		}
	})
	b.SetDeadline(time.Now().Add(3 * time.Second))
	return b, done
}
func send(t *testing.T, c net.Conn, f proto.Frame) {
	t.Helper()
	if e := proto.WriteFrame(c, f); e != nil {
		t.Fatal(e)
	}
}
func openStream(t *testing.T, c net.Conn, id uint32, timeout int64) {
	t.Helper()
	b, _ := json.Marshal(proto.Open{Argv: []string{"xdotool", "type", "--file", "-"}, TimeoutMS: timeout})
	send(t, c, proto.Frame{Type: proto.TOpen, Stream: id, Payload: b})
}
func exit(t *testing.T, c net.Conn, id uint32) (int, string, string) {
	t.Helper()
	var out, err strings.Builder
	for i := 0; i < 100; i++ {
		f, e := proto.ReadFrame(c)
		if e != nil {
			t.Fatal(e)
		}
		if f.Stream != id {
			t.Fatalf("unexpected stream %d", f.Stream)
		}
		switch f.Type {
		case proto.TStdout:
			out.Write(f.Payload)
		case proto.TStderr:
			err.Write(f.Payload)
		case proto.TExit:
			var x proto.Exit
			if json.Unmarshal(f.Payload, &x) != nil {
				t.Fatal("bad exit")
			}
			return x.RC, out.String(), err.String()
		}
	}
	t.Fatal("no exit")
	return 0, "", ""
}
func TestStdinStdoutRC(t *testing.T) {
	c, _ := start(t, "echo")
	openStream(t, c, 1, 0)
	send(t, c, proto.Frame{Type: proto.TStdin, Stream: 1, Payload: []byte("exact bytes\x00\n")})
	send(t, c, proto.Frame{Type: proto.TStdinEOF, Stream: 1})
	rc, out, err := exit(t, c, 1)
	if rc != 4 || out != "exact bytes\x00\n" || err != "tail" {
		t.Fatalf("%d %q %q", rc, out, err)
	}
}
func TestCancel(t *testing.T) {
	c, _ := start(t, "wait")
	openStream(t, c, 1, 0)
	send(t, c, proto.Frame{Type: proto.TCancel, Stream: 1})
	rc, _, _ := exit(t, c, 1)
	if rc != 6 {
		t.Fatal(rc)
	}
}
func TestTimeout(t *testing.T) {
	c, _ := start(t, "wait")
	openStream(t, c, 1, 30)
	rc, _, _ := exit(t, c, 1)
	if rc != 6 {
		t.Fatal(rc)
	}
}
func TestRefusals(t *testing.T) {
	c, _ := start(t, "echo")
	send(t, c, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: []byte(`{"argv":["sh"]}`)})
	rc, _, _ := exit(t, c, 1)
	if rc != 2 {
		t.Fatal(rc)
	}
	send(t, c, proto.Frame{Type: proto.TOpen, Stream: 2, Payload: []byte(`{`)})
	rc, _, _ = exit(t, c, 2)
	if rc != 5 {
		t.Fatal(rc)
	}
}
func TestStdinCap(t *testing.T) {
	c, _ := start(t, "wait")
	openStream(t, c, 1, 0)
	send(t, c, proto.Frame{Type: proto.TStdin, Stream: 1, Payload: make([]byte, 16385)})
	rc, _, _ := exit(t, c, 1)
	if rc != 5 {
		t.Fatal(rc)
	}
}
func TestConcurrentStreams(t *testing.T) {
	c, _ := start(t, "wait")
	openStream(t, c, 1, 0)
	openStream(t, c, 2, 0)
	send(t, c, proto.Frame{Type: proto.TCancel, Stream: 2})
	rc, _, _ := exit(t, c, 2)
	if rc != 6 {
		t.Fatal(rc)
	}
	send(t, c, proto.Frame{Type: proto.TCancel, Stream: 1})
	rc, _, _ = exit(t, c, 1)
	if rc != 6 {
		t.Fatal(rc)
	}
}

func TestDeadPeerStopsStreams(t *testing.T) {
	a, b := net.Pipe()
	done := make(chan error, 1)
	go func() { done <- serve(context.Background(), a, fakeCommand("wait"), []string{"SRF_AGENT_TEST=1"}) }()
	b.SetDeadline(time.Now().Add(3 * time.Second))
	openStream(t, b, 1, 0)
	b.Close()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("dead peer leaked stream")
	}
}
func TestUnresponsivePeerBounded(t *testing.T) {
	a, b := net.Pipe()
	done := make(chan error, 1)
	go func() { done <- serve(context.Background(), a, fakeCommand("flood"), []string{"SRF_AGENT_TEST=1"}) }()
	b.SetWriteDeadline(time.Now().Add(time.Second))
	openStream(t, b, 1, 0)
	defer b.Close()
	select {
	case <-done:
	case <-time.After(4 * time.Second):
		t.Fatal("writer did not terminate on blocked peer")
	}
}
func TestDuplicateOpen(t *testing.T) {
	c, _ := start(t, "wait")
	openStream(t, c, 1, 0)
	openStream(t, c, 1, 0)
	rc, _, _ := exit(t, c, 1)
	if rc != 5 {
		t.Fatal(rc)
	}
}
func TestMalformedFrameTerminates(t *testing.T) {
	c, _ := start(t, "wait")
	if _, e := c.Write([]byte("VMA1\xff\x00\x00\x00\x07\x00\x00\x00\x00")); e != nil {
		t.Fatal(e)
	}
	rc, _, _ := exit(t, c, 7)
	if rc != 5 {
		t.Fatal(rc)
	}
}

func TestMatchedStdinLimit(t *testing.T) {
	for _, tc := range []struct {
		argv []string
		cap  int64
		want int64
	}{
		{[]string{"xdotool", "search", "--class", "type"}, 0, 0},
		{[]string{"xdotool", "search", "--class", "type", "getwindowfocus"}, 0, 0},
		{[]string{"/usr/bin/xdotool", "type", "--file", "-"}, 0, 8000},
		{[]string{"xdotool", "type", "--file", "-"}, 7, 7},
		{[]string{"srf-fbread", "pair", ":100", "0"}, 0, 0},
	} {
		o := proto.Open{Argv: tc.argv, StdinCap: tc.cap}
		if _, err := proto.Allowed(o, nil); err != nil {
			t.Fatal(err)
		}
		if got := stdinLimit(o); got != tc.want {
			t.Errorf("%q: cap %d want %d", tc.argv, got, tc.want)
		}
	}
}

func TestNonTypeRejectsStdin(t *testing.T) {
	c, _ := start(t, "wait")
	payload, _ := json.Marshal(proto.Open{Argv: []string{"xdotool", "search", "--class", "type"}})
	send(t, c, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: payload})
	send(t, c, proto.Frame{Type: proto.TStdin, Stream: 1, Payload: []byte("x")})
	rc, _, _ := exit(t, c, 1)
	if rc != 5 {
		t.Fatalf("rc %d want oversize refusal", rc)
	}
}
