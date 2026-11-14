// Package w8muse drives registered bridge attempts through the host seat.
// It never runs task gates on the host or acquires a Docker capability.
package w8muse

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"time"

	"opensuperficies.local/srf/src/workerbridge"
)

const MaxHeader = 1 << 20
const MaxBundle = 512 << 20
const MaxPatch = 1 << 20

// Request is the closed seat protocol; task text never becomes host argv.
type Request struct {
	Verb                   string          `json:"verb"`
	WorkerTask             json.RawMessage `json:"worker_task,omitempty"`
	BundleLen              int64           `json:"bundle_len,omitempty"`
	PatchLen               int64           `json:"patch_len,omitempty"`
	ObservedIntegrationTip string          `json:"observed_integration_tip,omitempty"`
	Handle                 string          `json:"handle,omitempty"`
}
type Response struct {
	ExitCode     int                    `json:"exit_code"`
	Reason       string                 `json:"reason,omitempty"`
	Handle       string                 `json:"handle,omitempty"`
	Ready        bool                   `json:"ready,omitempty"`
	Receipt      json.RawMessage        `json:"receipt,omitempty"`
	Artifacts    map[string]string      `json:"artifacts,omitempty"`
	BridgeResult *workerbridge.Envelope `json:"bridge_result"`
}
type Seat interface {
	Exchange(context.Context, Request, io.Reader, []byte) (Response, error)
}

func refusal(code int, reason, detail string) error {
	return &workerbridge.Reject{Code: code, Class: reason, Detail: detail}
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

// WriteFrame emits a bounded length-prefixed JSON header with no trailing bytes.
func WriteFrame(w io.Writer, v any) error {
	b, e := json.Marshal(v)
	if e != nil {
		return refusal(2, "input", "JSON frame")
	}
	if len(b) == 0 || len(b) > MaxHeader {
		return refusal(2, "input", "header cap")
	}
	var h [4]byte
	binary.BigEndian.PutUint32(h[:], uint32(len(b)))
	if e = writeAll(w, h[:]); e != nil {
		return e
	}
	return writeAll(w, b)
}
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
func ReadFrame(r io.Reader, v any) error {
	var h [4]byte
	if _, e := io.ReadFull(r, h[:]); e != nil {
		return e
	}
	n := binary.BigEndian.Uint32(h[:])
	if n == 0 || n > MaxHeader {
		return refusal(2, "input", "header cap")
	}
	b := make([]byte, n)
	if _, e := io.ReadFull(r, b); e != nil {
		return e
	}
	value, e := workerbridge.Decode(b)
	if e != nil {
		return refusal(2, "input", "ambiguous JSON frame")
	}
	if _, ok := v.(*Response); ok {
		object, ok := value.(map[string]any)
		if !ok {
			return refusal(5, "verification", "seat reply is not an object")
		}
		code, ok := object["exit_code"].(json.Number)
		if !ok || !(code == "0" || code == "2" || code == "3" || code == "4" || code == "5" || code == "6" || code == "7") {
			return refusal(5, "verification", "seat reply exit_code absent or invalid")
		}
	}
	// Seat versions can add no unreviewed wire fields.
	return strictJSON(b, v)
}

// SocketSeat opens one bounded UDS exchange per verb, never retries internally.
type SocketSeat struct {
	Path    string
	Timeout time.Duration
}

func (s SocketSeat) Exchange(ctx context.Context, req Request, bundle io.Reader, patch []byte) (Response, error) {
	var reply Response
	if req.BundleLen < 0 || req.BundleLen > MaxBundle || req.PatchLen < 0 || req.PatchLen > MaxPatch || int64(len(patch)) != req.PatchLen {
		return reply, refusal(2, "input", "stream cap")
	}
	if req.Verb != "submit" && (req.BundleLen != 0 || req.PatchLen != 0 || req.WorkerTask != nil || req.ObservedIntegrationTip != "") {
		return reply, refusal(2, "input", "non-submit payload")
	}
	switch req.Verb {
	case "submit", "wait", "receipt", "ack", "cancel", "health":
	default:
		return reply, refusal(2, "input", "verb")
	}
	d := net.Dialer{}
	conn, e := d.DialContext(ctx, "unix", s.Path)
	if e != nil {
		return reply, e
	}
	defer conn.Close()
	timeout := s.Timeout
	if timeout <= 0 || timeout > 30*time.Second {
		timeout = 30 * time.Second
	}
	deadline := time.Now().Add(timeout)
	if t, ok := ctx.Deadline(); ok && t.Before(deadline) {
		deadline = t
	}
	_ = conn.SetDeadline(deadline)
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	if e = WriteFrame(conn, req); e != nil {
		return reply, e
	}
	if req.BundleLen > 0 {
		if bundle == nil {
			return reply, refusal(2, "input", "missing bundle")
		}
		if _, e = io.CopyN(conn, bundle, req.BundleLen); e != nil {
			return earlyReply(conn, e)
		}
	}
	if e = writeAll(conn, patch); e != nil {
		return earlyReply(conn, e)
	}
	if e = ReadFrame(conn, &reply); e != nil {
		return reply, e
	}
	if reply.ExitCode != 0 && (reply.ExitCode < 2 || reply.ExitCode > 7) {
		return Response{}, refusal(5, "verification", "invalid seat exit code")
	}
	return reply, nil
}

// earlyReply reads a seat refusal sent before the streams were consumed: the seat
// answers and closes, so the driver's write fails. That reply is authoritative; only
// a missing or invalid reply leaves the original transport error (seat_lost).
func earlyReply(conn net.Conn, writeErr error) (Response, error) {
	var reply Response
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	if e := ReadFrame(conn, &reply); e != nil || reply.ExitCode == 0 {
		return Response{}, writeErr
	}
	return reply, nil
}
