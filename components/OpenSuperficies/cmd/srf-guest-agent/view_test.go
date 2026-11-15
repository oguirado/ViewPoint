package main

import (
	"context"
	"io"
	"net"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestViewMuxIsolation(t *testing.T) {
	path := shortSocketPath(t, "view-test.sock")
	os.Remove(path)
	l, e := net.Listen("unix", path)
	if e != nil {
		t.Fatal(e)
	}
	defer l.Close()
	defer os.Remove(path)
	accepted := make(chan net.Conn, 2)
	go func() {
		for i := 0; i < 2; i++ {
			c, e := l.Accept()
			if e != nil {
				return
			}
			accepted <- c
		}
	}()
	a, b := net.Pipe()
	done := make(chan error, 1)
	go func() { done <- serveView(context.Background(), a, path) }()
	defer func() {
		b.Close()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("view server leaked")
		}
	}()
	b.SetDeadline(time.Now().Add(3 * time.Second))
	send(t, b, proto.Frame{Type: proto.TViewOpen, Stream: 10})
	send(t, b, proto.Frame{Type: proto.TViewOpen, Stream: 20})
	var first, second net.Conn
	select {
	case first = <-accepted:
	case <-time.After(time.Second):
		t.Fatal("first dial")
	}
	defer first.Close()
	select {
	case second = <-accepted:
	case <-time.After(time.Second):
		t.Fatal("second dial")
	}
	defer second.Close()
	first.SetDeadline(time.Now().Add(2 * time.Second))
	second.SetDeadline(time.Now().Add(2 * time.Second))
	send(t, b, proto.Frame{Type: proto.TViewData, Stream: 20, Payload: []byte("request")})
	buf := make([]byte, 7)
	if _, e := io.ReadFull(second, buf); e != nil || string(buf) != "request" {
		t.Fatalf("%q %v", buf, e)
	}
	first.Write([]byte("first"))
	f, e := proto.ReadFrame(b)
	if e != nil || f.Type != proto.TViewData || f.Stream != 10 || string(f.Payload) != "first" {
		t.Fatalf("%v %v", f, e)
	}
	second.Write([]byte("second"))
	f, e = proto.ReadFrame(b)
	if e != nil || f.Stream != 20 || string(f.Payload) != "second" {
		t.Fatalf("%v %v", f, e)
	}
	send(t, b, proto.Frame{Type: proto.TViewClose, Stream: 10})
	f, e = proto.ReadFrame(b)
	if e != nil || f.Type != proto.TViewClose || f.Stream != 10 {
		t.Fatalf("%v %v", f, e)
	}
}

func TestViewReopenLiveID(t *testing.T) {
	path := shortSocketPath(t, "view-reopen.sock")
	os.Remove(path)
	l, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(path)
	defer l.Close()
	accepted := make(chan net.Conn, 2)
	go func() {
		for i := 0; i < 2; i++ {
			c, e := l.Accept()
			if e != nil {
				return
			}
			accepted <- c
		}
	}()
	a, b := net.Pipe()
	done := make(chan error, 1)
	go func() { done <- serveView(context.Background(), a, path) }()
	defer func() {
		b.Close()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("server leaked")
		}
	}()
	b.SetDeadline(time.Now().Add(4 * time.Second))
	send(t, b, proto.Frame{Type: proto.TViewOpen, Stream: 1})
	first := <-accepted
	defer first.Close()
	send(t, b, proto.Frame{Type: proto.TViewOpen, Stream: 1})
	f, err := proto.ReadFrame(b)
	if err != nil || f.Type != proto.TViewClose || f.Stream != 1 {
		t.Fatalf("old close: %v %v", f, err)
	}
	var second net.Conn
	select {
	case second = <-accepted:
	case <-time.After(time.Second):
		t.Fatal("replacement was not dialed")
	}
	defer second.Close()
	first.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := first.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("old connection still open: %v", err)
	}
	send(t, b, proto.Frame{Type: proto.TViewData, Stream: 1, Payload: []byte("new")})
	second.SetDeadline(time.Now().Add(time.Second))
	buf := make([]byte, 3)
	if _, err := io.ReadFull(second, buf); err != nil || string(buf) != "new" {
		t.Fatalf("replacement input: %q %v", buf, err)
	}
	second.Write([]byte("reply"))
	f, err = proto.ReadFrame(b)
	if err != nil || f.Type != proto.TViewData || string(f.Payload) != "reply" {
		t.Fatalf("replacement output: %v %v", f, err)
	}
	send(t, b, proto.Frame{Type: proto.TViewClose, Stream: 1})
	f, err = proto.ReadFrame(b)
	if err != nil || f.Type != proto.TViewClose {
		t.Fatalf("replacement close: %v %v", f, err)
	}
}

// shortSocketPath returns a socket path in a fresh short temporary directory (Unix socket paths are
// limited to 108 bytes; repository scratch paths are not available in every checkout).
func shortSocketPath(t *testing.T, name string) string {
	t.Helper()
	dir, e := os.MkdirTemp("", "sga")
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	// Keep sockets in the required scratch TMPDIR without exceeding sockaddr_un
	// when the worktree's absolute path is long.
	cwd, e := os.Getwd()
	if e != nil {
		t.Fatal(e)
	}
	rel, e := filepath.Rel(cwd, dir)
	if e != nil {
		t.Fatal(e)
	}
	return filepath.Join(rel, name)
}
