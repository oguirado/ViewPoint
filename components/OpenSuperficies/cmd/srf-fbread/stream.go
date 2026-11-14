package main

// Resident W8U pair stream sender (W8U_DESIGN.md U3). One private X
// connection for the whole run; host control on stdin; W8UH, W8UF and W8UE
// records on stdout; one bounded diagnostic line on stderr. Nothing is ever
// written to a file.

import (
	"crypto/sha256"
	"errors"
	"io"
	"strings"
	"time"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/ramcap"
)

const streamDisplay = ":100"

func monoNow() uint64 {
	var ts unix.Timespec
	if unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts) != nil {
		return 0
	}
	return uint64(ts.Sec)*1_000_000_000 + uint64(ts.Nsec)
}

type controlItem struct {
	rec ramcap.ControlRecord
	err error
}

type streamSender struct {
	xid     uint32
	stdout  io.Writer
	stderr  io.Writer
	d       dependencies
	binding ramcap.SeatBinding
	bound   bool
	ledger  *ramcap.CreditLedger
}

// runStream returns 0 after a clean STOP and 5 after any refused control,
// capture or write; argv errors were already rc 2.
func runStream(xid uint32, stdout, stderr io.Writer, d dependencies) int {
	if d.stdin == nil || d.open == nil || d.read == nil {
		_, _ = io.WriteString(stderr, "E_WIRE: stream dependencies missing\n")
		return 2
	}
	if d.mono == nil {
		d.mono = monoNow
	}
	s := &streamSender{xid: xid, stdout: stdout, stderr: stderr, d: d}
	ctrl := make(chan controlItem)
	done := make(chan struct{})
	defer close(done)
	go func() {
		for {
			rec, err := ramcap.ReadControl(d.stdin, ramcap.MagicControl)
			select {
			case ctrl <- controlItem{rec, err}:
			case <-done:
				return
			}
			if err != nil {
				return
			}
		}
	}()
	return s.serve(ctrl)
}

func (s *streamSender) serve(ctrl <-chan controlItem) int {
	// Unbound: only an authenticated HELLO is admissible; it grants no credit.
	if err := s.hello(<-ctrl); err != nil {
		return s.end(err)
	}
	srv, err := s.d.open()
	if err != nil {
		return s.end(stdErr(ramcap.StreamWire, "open X: "+err.Error()))
	}
	defer srv.Close()
	confirm := ramcap.EncodeControl(ramcap.MagicHelloConfirm, ramcap.KindHello, s.binding.SHA256, nil)
	if !s.write(confirm) {
		return 5
	}
	s.ledger = ramcap.NewCreditLedger(s.binding.MaxInflightFrames)
	for {
		var it controlItem
		if s.ledger.CanSpend() {
			// Pending controls (STOP in particular) win over the next capture.
			select {
			case it = <-ctrl:
			default:
				if rc, ok := s.frame(srv); !ok {
					return rc
				}
				continue
			}
		} else {
			// Credit wait happens here, never inside the X grab.
			it = <-ctrl
		}
		stop, err := s.control(it)
		if err != nil {
			return s.end(err)
		}
		if stop {
			return s.end(nil)
		}
	}
}

func (s *streamSender) hello(it controlItem) error {
	if it.err != nil {
		return controlErr(it.err)
	}
	if it.rec.Kind != ramcap.KindHello {
		return stdErr(ramcap.StreamWire, "first control is not HELLO")
	}
	if sha256.Sum256(it.rec.Body) != it.rec.BindingSHA {
		return stdErr(ramcap.StreamBinding, "HELLO digest")
	}
	b, err := ramcap.ParseSeatBinding(it.rec.Body)
	if err != nil {
		return err
	}
	if b.XID != s.xid || b.Display != streamDisplay {
		return stdErr(ramcap.StreamBinding, "display/xid differ from reader argv")
	}
	if b.TickMode != ramcap.TickModeUnavailable {
		return stdErr(ramcap.StreamTick, "tick_mode committed needs engine coordination (unsupported)")
	}
	s.binding, s.bound = b, true
	return nil
}

// control applies one bound-state control record; true means STOP.
func (s *streamSender) control(it controlItem) (bool, error) {
	if it.err != nil {
		return false, controlErr(it.err)
	}
	switch it.rec.Kind {
	case ramcap.KindAck:
		if it.rec.BindingSHA != s.binding.SHA256 {
			return false, stdErr(ramcap.StreamCredit, "ACK binding digest")
		}
		ackSeq, total, err := ramcap.DecodeAckBody(it.rec.Body)
		if err != nil {
			return false, err
		}
		return false, s.ledger.ApplyAck(ackSeq, total)
	case ramcap.KindStop:
		if it.rec.BindingSHA != s.binding.SHA256 {
			return false, stdErr(ramcap.StreamBinding, "STOP binding digest")
		}
		return true, nil
	}
	return false, stdErr(ramcap.StreamWire, "HELLO after bind")
}

// frame acquires one credit, captures a grab-held pair, ungrabs, validates,
// then performs the (possibly blocking) write. ok=false means the run ended.
func (s *streamSender) frame(srv imageServer) (int, bool) {
	if err := s.ledger.Spend(); err != nil {
		return s.end(err), false
	}
	mono := s.d.mono()
	if err := renewDeadline(srv); err != nil {
		return s.end(captureErr(err)), false
	}
	a, b, err := capturePairLegs(srv, s.d.read, grabLimit, s.xid)
	if err != nil {
		return s.end(captureErr(err)), false
	}
	w, h := s.binding.Width, s.binding.Height
	_, sha, err := ramcap.PairLegs(a, b, w, h)
	if err != nil {
		return s.end(err), false
	}
	seq := s.ledger.Sent() + 1
	if rb := uint64(ramcap.FrameHeaderBytes + 16 + len(a) + len(b)); rb > s.binding.RecordLimit() {
		return s.end(stdErr(ramcap.StreamLength, "record above cap")), false
	}
	rec := ramcap.EncodeFrame(seq, 0, mono, ramcap.FlagPairUnderGrab, sha, s.binding.SHA256, w, h, a, b)
	if !s.write(rec) {
		// A partial write terminates the incarnation; it is never retried.
		return 5, false
	}
	if err := s.ledger.MarkSent(seq); err != nil {
		return s.end(err), false
	}
	return 0, true
}

func (s *streamSender) write(b []byte) bool {
	n, err := s.stdout.Write(b)
	return err == nil && n == len(b)
}

// end emits W8UE {last_seq, sent, error_code} and returns the exit code.
func (s *streamSender) end(err error) int {
	var sent uint64
	if s.ledger != nil {
		sent = s.ledger.Sent()
	}
	var sha [32]byte
	if s.bound {
		sha = s.binding.SHA256
	}
	code := ramcap.StreamCodeOf(err)
	body := ramcap.EndBody{LastSeq: sent, Sent: sent, ErrorCode: code}.Encode()
	ok := s.write(ramcap.EncodeControl(ramcap.MagicEnd, ramcap.KindEnd, sha, body))
	if err != nil {
		msg := err.Error()
		if len(msg) > 1024 {
			msg = msg[:1024]
		}
		_, _ = io.WriteString(s.stderr, msg+"\n")
		return 5
	}
	if !ok {
		return 5
	}
	return 0
}

func stdErr(c ramcap.StreamCode, detail string) error {
	return &ramcap.StreamError{Code: c, Detail: detail}
}

func controlErr(err error) error {
	if errors.Is(err, io.EOF) {
		return stdErr(ramcap.StreamWire, "control channel closed without STOP")
	}
	return err
}

// captureErr maps reader errors onto the stream table: a failed ungrab means
// the pair cannot be shown to be grab-held, so it is E_TORN like a leg that
// changed between reads.
func captureErr(err error) error {
	msg := err.Error()
	switch {
	case strings.HasPrefix(msg, "E_TORN"), strings.HasPrefix(msg, "E_XWD_FORMAT: ungrab"):
		return stdErr(ramcap.StreamTorn, msg)
	case strings.HasPrefix(msg, "E_TIMEOUT"):
		return stdErr(ramcap.StreamTimeout, msg)
	case strings.HasPrefix(msg, "E_XWD_GEOMETRY"):
		return stdErr(ramcap.StreamGeometry, msg)
	}
	return stdErr(ramcap.StreamWire, msg)
}

// renewDeadline re-arms the X socket deadline before each pair: the resident
// connection outlives openServer's one-shot absolute deadline.
func renewDeadline(srv imageServer) error {
	if r, ok := srv.(interface{ Renew(time.Duration) error }); ok {
		return r.Renew(2 * time.Second)
	}
	return nil
}
