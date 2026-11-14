package proto

import (
	"encoding/binary"
	"io"
	"strings"
)

func malformed() error      { return &Error{5, "malformed VM agent frame"} }
func validType(t Type) bool { return t >= TOpen && t <= TViewClose }
func writeAll(w io.Writer, b []byte) error {
	for len(b) > 0 {
		n, e := w.Write(b)
		if e != nil {
			return e
		}
		if n <= 0 || n > len(b) {
			return io.ErrShortWrite
		}
		b = b[n:]
	}
	return nil
}
func writeFrame(w io.Writer, f Frame) error {
	if !validType(f.Type) || len(f.Payload) > MaxPayload {
		return malformed()
	}
	var h [13]byte
	copy(h[:4], Magic)
	h[4] = byte(f.Type)
	binary.BigEndian.PutUint32(h[5:9], f.Stream)
	binary.BigEndian.PutUint32(h[9:], uint32(len(f.Payload)))
	if e := writeAll(w, h[:]); e != nil {
		return e
	}
	return writeAll(w, f.Payload)
}
func readFrame(r io.Reader) (Frame, error) {
	var h [13]byte
	f := Frame{}
	n, e := io.ReadFull(r, h[:])
	if e != nil {
		if n == 0 && e == io.EOF {
			return f, io.EOF
		}
		return f, malformed()
	}
	f.Stream = binary.BigEndian.Uint32(h[5:9])
	f.Type = Type(h[4])
	size := binary.BigEndian.Uint32(h[9:])
	if string(h[:4]) != Magic || !validType(f.Type) || size > MaxPayload {
		return f, malformed()
	}
	f.Payload = make([]byte, size)
	if _, e = io.ReadFull(r, f.Payload); e != nil {
		return f, malformed()
	}
	return f, nil
}
func forwardEnv(env []string) map[string]string {
	out := map[string]string{}
	for _, v := range env {
		k, s, ok := strings.Cut(v, "=")
		if !ok {
			continue
		}
		if (k == "DISPLAY" && s == ":100") || ((k == "NO_AT_BRIDGE" || k == "SRF_LOGIN") && (s == "0" || s == "1")) {
			out[k] = s
		}
	}
	return out
}
