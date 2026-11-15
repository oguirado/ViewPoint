package proto

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"reflect"
	"testing"
)

func code(t *testing.T, err error, want int) {
	t.Helper()
	var e *Error
	if !errors.As(err, &e) || e.Code != want {
		t.Fatalf("error=%v want code %d", err, want)
	}
}
func TestCodec(t *testing.T) {
	f := Frame{TStdout, 42, []byte("hello")}
	var b bytes.Buffer
	if e := WriteFrame(&b, f); e != nil {
		t.Fatal(e)
	}
	if got := b.Bytes(); !bytes.Equal(got[:4], []byte("VMA1")) || binary.BigEndian.Uint32(got[5:9]) != 42 {
		t.Fatal("wire header")
	}
	g, e := ReadFrame(&b)
	if e != nil || !reflect.DeepEqual(f, g) {
		t.Fatalf("%v %v", g, e)
	}
	if _, e = ReadFrame(&b); e != io.EOF {
		t.Fatal(e)
	}
	for _, f := range []Frame{{Type: 99}, {Type: TStdin, Payload: make([]byte, MaxPayload+1)}} {
		code(t, WriteFrame(io.Discard, f), 5)
	}
	for _, raw := range [][]byte{[]byte("BAD!\x01\x00\x00\x00\x01\x00\x00\x00\x00"), []byte("VMA1\xff\x00\x00\x00\x01\x00\x00\x00\x00"), []byte("VMA1\x01\x00\x00\x00\x01\x00\x10\x00\x01"), []byte("VMA1\x01")} {
		_, e := ReadFrame(bytes.NewReader(raw))
		code(t, e, 5)
	}
}
func TestAllowed(t *testing.T) {
	good := [][]string{
		{"xdotool", "getwindowgeometry", "--shell", "0x123"}, {"xdotool", "getwindowfocus"}, {"xdotool", "windowactivate", "--sync", "123"},
		{"xdotool", "key", "--clearmodifiers", "ctrl+v"}, {"xdotool", "mousemove", "1", "2", "mousemove", "--sync", "2", "2", "click", "--clearmodifiers", "1"},
		{"xdotool", "keydown", "ctrl", "mousemove", "1", "2", "mousemove", "--sync", "2", "2", "click", "--repeat", "3", "--delay", "40", "4", "keyup", "ctrl"},
		{"xdotool", "mousemove", "1", "2", "mousedown", "1", "sleep", "0.03", "mousemove", "3", "4", "sleep", "0.05", "mouseup", "1"},
		{"xdotool", "type", "--clearmodifiers", "--delay", "0", "--file", "-"}, {"xdotool", "getwindowname", "123"},
		{"xdotool", "search", "--onlyvisible", "--class", "App"}, {"xdotool", "windowmove", "123", "0", "2", "windowsize", "123", "400", "300"},
		{"/usr/local/libexec/srf-fbread", "pair", ":100", "0"}, {"/usr/local/bin/identity-entrypoint.sh", "xpra-start"}, {"identity-entrypoint.sh", "xpra-stop"},
	}
	for _, a := range good {
		if _, e := Allowed(Open{Argv: a}, nil); e != nil {
			t.Errorf("%q: %v", a, e)
		}
	}
	for _, a := range [][]string{nil, {"sh", "-c", "true"}, {"/tmp/xdotool", "key", "a"}, {"xdotool", "exec", "sh"}, {"xdotool", "key", "--window", "1", "a"}, {"xdotool", "click", "0"}, {"xdotool", "sleep", "NaN"}, {"srf-fbread", "pair", ":101", "0"}, {"python3", "-I", "-c", "evil"}, {"identity-entrypoint.sh", "boot"}} {
		_, e := Allowed(Open{Argv: a}, nil)
		code(t, e, 2)
	}
	_, e := Allowed(Open{Argv: []string{"xdotool", "getwindowfocus"}, TimeoutMS: 99999}, nil)
	code(t, e, 2)
}
func TestForwardEnv(t *testing.T) {
	g := ForwardEnv([]string{"DISPLAY=:0", "SRF_LOGIN=1", "NO_AT_BRIDGE=0", "PATH=evil", "DISPLAY=:100"})
	w := map[string]string{"DISPLAY": ":100", "SRF_LOGIN": "1", "NO_AT_BRIDGE": "0"}
	if !reflect.DeepEqual(g, w) {
		t.Fatal(g)
	}
}
