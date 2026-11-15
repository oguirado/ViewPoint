package ramcap

// W8U resident pair stream, wire version 1 (W8U_DESIGN.md section U3).
//
// Every scalar is unsigned big endian. Downstream (capsule stdout) carries one
// W8UH HELLO confirmation, zero or more W8UF FRAME records and one terminal
// W8UE END record followed by EOF. Upstream (host stdin) carries W8UC control
// records: HELLO (kind 1), ACK (kind 2) and STOP (kind 3).
//
// FRAME header, 128 bytes:
//
//	off  0  4  magic "W8UF"
//	off  4  2  version = 1
//	off  6  2  header_bytes = 128
//	off  8  8  record_bytes = 128 + payload_bytes
//	off 16  8  seq, 1-based, exactly previous+1
//	off 24  8  tick (0 unless flags bit1 tick_valid)
//	off 32  8  capture_mono_ns
//	off 40  4  width
//	off 44  4  height
//	off 48  4  stride = width*3
//	off 52  2  pixel_format = 1 (canonical RGB24)
//	off 54  2  flags: bit0 pair_under_grab (required), bit1 tick_valid
//	off 56  8  payload_bytes = record_bytes-128
//	off 64 32  sha256 of leg A canonical RGB24
//	off 96 32  binding_sha256
//	off 128    payload: u64 lenA, XWD A, u64 lenB, XWD B
//
// Control header, 44 bytes, shared by W8UC, W8UH and W8UE:
//
//	off  0  4  magic
//	off  4  2  version = 1
//	off  6  2  kind
//	off  8  4  body_bytes
//	off 12 32  binding_sha256
//	off 44     body
//
// Resolved ambiguities (this implementation's choices, documented for the
// Python peer): W8UH uses kind 1 with an empty body and the binding digest in
// the header; W8UE uses kind 4 with body {last_seq u64, sent u64, error_code
// u32}; an END emitted before a HELLO was accepted carries an all-zero binding
// digest. A truncated fixed header is E_WIRE; a truncated advertised body or
// payload, and any byte after END, is E_LENGTH.

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	MagicFrame        = "W8UF"
	MagicControl      = "W8UC"
	MagicHelloConfirm = "W8UH"
	MagicEnd          = "W8UE"

	WireVersion        = 1
	FrameHeaderBytes   = 128
	ControlHeaderBytes = 44

	KindHello = 1
	KindAck   = 2
	KindStop  = 3
	KindEnd   = 4

	MaxHelloBodyBytes = 16 << 10 // [TARGET]
	AckBodyBytes      = 16
	EndBodyBytes      = 20

	PixelFormatRGB24  = 1
	FlagPairUnderGrab = 1 << 0
	FlagTickValid     = 1 << 1
	knownFlags        = FlagPairUnderGrab | FlagTickValid

	InitialCreditFrames = 2       // [TARGET] first ACK grants this capacity
	LegSlackBytes       = 1 << 20 // [TARGET] per-leg allowance above 4*w*h
	MaxStreamDim        = 16384

	StreamFramesRoot = "/dev/shm/vqa-ramws"
	StreamRunParent  = StreamFramesRoot + "/opensuperficies/os-cu-0"

	TickModeUnavailable = "unavailable"
	TickModeCommitted   = "committed"
)

// StreamCode is the numeric error_code carried by W8UE. 0 is a clean STOP.
type StreamCode uint32

const (
	StreamOK           StreamCode = 0
	StreamTorn         StreamCode = 1
	StreamWire         StreamCode = 2
	StreamLength       StreamCode = 3
	StreamHash         StreamCode = 4
	StreamSeq          StreamCode = 5
	StreamTick         StreamCode = 6
	StreamBinding      StreamCode = 7
	StreamGeometry     StreamCode = 8
	StreamCredit       StreamCode = 9
	StreamBackpressure StreamCode = 10
	StreamTimeout      StreamCode = 11
	StreamRoot         StreamCode = 12
	StreamRAMQuota     StreamCode = 13
)

// StreamErrorTable is the single shared error_code table for sender and host.
var StreamErrorTable = []struct {
	Code    StreamCode
	Name    string
	Meaning string
}{
	{StreamOK, "OK", "clean STOP; no error"},
	{StreamTorn, "E_TORN", "pair legs differ in canonical RGB24, a leg changed between reads, or the grab could not be released"},
	{StreamWire, "E_WIRE", "bad magic, version, header size, kind, flags, pixel format, record order or XWD leg format"},
	{StreamLength, "E_LENGTH", "advertised length above cap, inconsistent lengths, truncated body/payload or bytes after END"},
	{StreamHash, "E_HASH", "header sha256_rgb24 differs from the recomputed canonical digest"},
	{StreamSeq, "E_SEQ", "seq not exactly previous+1 from 1, or END counters disagree"},
	{StreamTick, "E_TICK", "tick_valid/tick inconsistent with tick_mode, or committed tick mode unsupported"},
	{StreamBinding, "E_BINDING", "binding JSON not canonical/closed, digest mismatch, or identity differs from the reader"},
	{StreamGeometry, "E_GEOMETRY", "leg or header geometry/stride differs from the binding"},
	{StreamCredit, "E_CREDIT", "ACK regression, unsent seq, inflation, wrong binding, overflow, or frame without credit"},
	{StreamBackpressure, "E_BACKPRESSURE", "host consumer or credit deadline expired"},
	{StreamTimeout, "E_TIMEOUT", "X server grab deadline or channel deadline expired"},
	{StreamRoot, "E_ROOT", "frames_root/frame_run_root differ from the single host frame root convention"},
	{StreamRAMQuota, "E_RAM_QUOTA", "RAM reservation for frames unavailable"},
}

// StreamCodeName returns the E_* name, or "" for an unknown code.
func StreamCodeName(c StreamCode) string {
	for _, e := range StreamErrorTable {
		if e.Code == c {
			return e.Name
		}
	}
	return ""
}

// StreamError carries a table code and a bounded detail.
type StreamError struct {
	Code   StreamCode
	Detail string
}

func (e *StreamError) Error() string {
	if e.Detail == "" {
		return StreamCodeName(e.Code)
	}
	return StreamCodeName(e.Code) + ": " + e.Detail
}

func streamErr(c StreamCode, format string, a ...any) error {
	d := fmt.Sprintf(format, a...)
	if len(d) > 512 {
		d = d[:512]
	}
	return &StreamError{Code: c, Detail: d}
}

// StreamCodeOf maps an error to its table code; non-stream errors are E_WIRE.
func StreamCodeOf(err error) StreamCode {
	if err == nil {
		return StreamOK
	}
	var se *StreamError
	if errors.As(err, &se) {
		return se.Code
	}
	return StreamWire
}

// RecordCap is 2*(4*w*h + 1 MiB) + two inner u64 length words + outer header.
func RecordCap(w, h int) uint64 {
	return 2*legCap(w, h) + 16 + FrameHeaderBytes
}

func legCap(w, h int) uint64 { return 4*uint64(w)*uint64(h) + LegSlackBytes }

// ---------------------------------------------------------------- binding

// SeatBinding is a validated canonical HELLO binding.
type SeatBinding struct {
	Canonical         []byte
	SHA256            [32]byte
	RunID             string
	Display           string
	FramesRoot        string
	FrameRunRoot      string
	TickMode          string
	XID               uint32
	Width, Height     int
	MaxRecordBytes    uint64
	MaxInflightFrames uint64
}

// RecordLimit is the smaller of the geometry cap and the negotiated cap.
func (b SeatBinding) RecordLimit() uint64 {
	return min(RecordCap(b.Width, b.Height), b.MaxRecordBytes)
}

var bindingStringKeys = []string{"container_id", "display", "exec_allowlist_sha256", "frame_run_root", "frames_root", "image_sha256", "lease_id", "reader_sha256", "run_id", "schema", "seat_grant_sha256", "tick_mode"}
var bindingIntKeys = []string{"epoch", "height", "max_record_bytes", "max_inflight_frames", "width", "xid"}
var runIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// CanonicalJSON encodes maps (sorted keys), strings and integers compactly
// with no trailing LF. Strings must be printable ASCII so the encoding is
// identical to Python json.dumps(sort_keys=True, separators=(",", ":")).
func CanonicalJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := canon(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func canon(buf *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := canonString(buf, k); err != nil {
				return err
			}
			buf.WriteByte(':')
			if err := canon(buf, x[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	case string:
		return canonString(buf, x)
	case json.Number:
		s := string(x)
		if _, err := strconv.ParseInt(s, 10, 64); err != nil {
			if _, err2 := strconv.ParseUint(s, 10, 64); err2 != nil {
				return errors.New("non-integer number")
			}
		}
		buf.WriteString(s)
	case int:
		buf.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		buf.WriteString(strconv.FormatInt(x, 10))
	case uint32:
		buf.WriteString(strconv.FormatUint(uint64(x), 10))
	case uint64:
		buf.WriteString(strconv.FormatUint(x, 10))
	default:
		return fmt.Errorf("unsupported JSON type %T", v)
	}
	return nil
}

func canonString(buf *bytes.Buffer, s string) error {
	buf.WriteByte('"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < 0x20 || c > 0x7e {
			return errors.New("string not printable ASCII")
		}
		if c == '"' || c == '\\' {
			buf.WriteByte('\\')
		}
		buf.WriteByte(c)
	}
	buf.WriteByte('"')
	return nil
}

// ParseSeatBinding accepts only the exact canonical closed binding object.
func ParseSeatBinding(body []byte) (SeatBinding, error) {
	var b SeatBinding
	if len(body) > MaxHelloBodyBytes {
		return b, streamErr(StreamLength, "binding %d bytes", len(body))
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var raw map[string]any
	if err := dec.Decode(&raw); err != nil || dec.More() {
		return b, streamErr(StreamBinding, "binding JSON")
	}
	again, err := CanonicalJSON(raw)
	if err != nil || !bytes.Equal(again, body) {
		return b, streamErr(StreamBinding, "binding not canonical")
	}
	if len(raw) != len(bindingStringKeys)+len(bindingIntKeys)+1 {
		return b, streamErr(StreamBinding, "binding key set")
	}
	str := map[string]string{}
	for _, k := range bindingStringKeys {
		s, ok := raw[k].(string)
		if !ok || s == "" {
			return b, streamErr(StreamBinding, "binding %s", k)
		}
		str[k] = s
	}
	num := map[string]uint64{}
	for _, k := range bindingIntKeys {
		n, ok := raw[k].(json.Number)
		if !ok {
			return b, streamErr(StreamBinding, "binding %s", k)
		}
		u, err := strconv.ParseUint(string(n), 10, 64)
		if err != nil {
			return b, streamErr(StreamBinding, "binding %s", k)
		}
		num[k] = u
	}
	th, ok := raw["target_handle"].(map[string]any)
	if !ok || len(th) != 3 {
		return b, streamErr(StreamBinding, "binding target_handle")
	}
	for _, k := range []string{"kind", "canonical_id"} {
		if s, ok := th[k].(string); !ok || s == "" {
			return b, streamErr(StreamBinding, "binding target_handle.%s", k)
		}
	}
	switch g := th["generation"].(type) {
	case string:
		if g == "" {
			return b, streamErr(StreamBinding, "binding target_handle.generation")
		}
	case json.Number:
	default:
		return b, streamErr(StreamBinding, "binding target_handle.generation")
	}
	if num["xid"] > math.MaxUint32 || num["width"] < 1 || num["width"] > MaxStreamDim || num["height"] < 1 || num["height"] > MaxStreamDim {
		return b, streamErr(StreamGeometry, "binding geometry")
	}
	if num["max_record_bytes"] == 0 || num["max_inflight_frames"] < 1 || num["max_inflight_frames"] > InitialCreditFrames {
		return b, streamErr(StreamBinding, "binding limits")
	}
	tm := str["tick_mode"]
	if tm != TickModeUnavailable && tm != TickModeCommitted {
		return b, streamErr(StreamBinding, "tick_mode")
	}
	runID := str["run_id"]
	if str["frames_root"] != StreamFramesRoot || !runIDPattern.MatchString(runID) || strings.Trim(runID, ".") == "" || str["frame_run_root"] != StreamRunParent+"/"+runID {
		return b, streamErr(StreamRoot, "frame root")
	}
	b = SeatBinding{
		Canonical: bytes.Clone(body), SHA256: sha256.Sum256(body),
		RunID: runID, Display: str["display"], FramesRoot: str["frames_root"], FrameRunRoot: str["frame_run_root"], TickMode: tm,
		XID: uint32(num["xid"]), Width: int(num["width"]), Height: int(num["height"]),
		MaxRecordBytes: num["max_record_bytes"], MaxInflightFrames: num["max_inflight_frames"],
	}
	return b, nil
}

// ---------------------------------------------------------------- control

// ControlRecord is one decoded control-header record.
type ControlRecord struct {
	Magic      string
	Kind       uint16
	BindingSHA [32]byte
	Body       []byte
}

// EncodeControl builds a control-header record.
func EncodeControl(magic string, kind uint16, sha [32]byte, body []byte) []byte {
	out := make([]byte, ControlHeaderBytes+len(body))
	copy(out, magic)
	binary.BigEndian.PutUint16(out[4:], WireVersion)
	binary.BigEndian.PutUint16(out[6:], kind)
	binary.BigEndian.PutUint32(out[8:], uint32(len(body)))
	copy(out[12:], sha[:])
	copy(out[ControlHeaderBytes:], body)
	return out
}

// controlBody returns (exact, n): the body size rule for a magic/kind pair.
func controlBody(magic string, kind uint16) (bool, uint32, bool) {
	switch {
	case magic == MagicControl && kind == KindHello:
		return false, MaxHelloBodyBytes, true
	case magic == MagicControl && kind == KindAck:
		return true, AckBodyBytes, true
	case magic == MagicControl && kind == KindStop:
		return true, 0, true
	case magic == MagicHelloConfirm && kind == KindHello:
		return true, 0, true
	case magic == MagicEnd && kind == KindEnd:
		return true, EndBodyBytes, true
	}
	return false, 0, false
}

// ReadControl reads one control record of the given magic. A clean EOF
// before the first byte returns io.EOF; every other failure is a StreamError.
// Magic, version, kind and length are checked before the body is allocated.
func ReadControl(r io.Reader, magic string) (ControlRecord, error) {
	var hdr [ControlHeaderBytes]byte
	n, err := io.ReadFull(r, hdr[:4])
	if n == 0 && err == io.EOF {
		return ControlRecord{}, io.EOF
	}
	if err != nil {
		return ControlRecord{}, streamErr(StreamWire, "truncated control header")
	}
	return readControlRest(r, hdr, magic)
}

func readControlRest(r io.Reader, hdr [ControlHeaderBytes]byte, magic string) (ControlRecord, error) {
	if string(hdr[:4]) != magic {
		return ControlRecord{}, streamErr(StreamWire, "magic %q", hdr[:4])
	}
	if _, err := io.ReadFull(r, hdr[4:]); err != nil {
		return ControlRecord{}, streamErr(StreamWire, "truncated control header")
	}
	if v := binary.BigEndian.Uint16(hdr[4:]); v != WireVersion {
		return ControlRecord{}, streamErr(StreamWire, "version %d", v)
	}
	kind := binary.BigEndian.Uint16(hdr[6:])
	size := binary.BigEndian.Uint32(hdr[8:])
	exact, limit, ok := controlBody(magic, kind)
	if !ok {
		return ControlRecord{}, streamErr(StreamWire, "kind %d", kind)
	}
	if (exact && size != limit) || size > limit {
		if exact {
			return ControlRecord{}, streamErr(StreamWire, "kind %d body %d", kind, size)
		}
		return ControlRecord{}, streamErr(StreamLength, "kind %d body %d", kind, size)
	}
	rec := ControlRecord{Magic: magic, Kind: kind, Body: make([]byte, size)}
	copy(rec.BindingSHA[:], hdr[12:])
	if _, err := io.ReadFull(r, rec.Body); err != nil {
		return ControlRecord{}, streamErr(StreamLength, "truncated control body")
	}
	return rec, nil
}

// EncodeAckBody / DecodeAckBody: ack_seq u64, cumulative_credit_total u64.
func EncodeAckBody(ackSeq, total uint64) []byte {
	b := make([]byte, AckBodyBytes)
	binary.BigEndian.PutUint64(b, ackSeq)
	binary.BigEndian.PutUint64(b[8:], total)
	return b
}
func DecodeAckBody(b []byte) (uint64, uint64, error) {
	if len(b) != AckBodyBytes {
		return 0, 0, streamErr(StreamWire, "ack body %d", len(b))
	}
	return binary.BigEndian.Uint64(b), binary.BigEndian.Uint64(b[8:]), nil
}

// EndBody is the W8UE body.
type EndBody struct {
	LastSeq, Sent uint64
	ErrorCode     StreamCode
}

func (e EndBody) Encode() []byte {
	b := make([]byte, EndBodyBytes)
	binary.BigEndian.PutUint64(b, e.LastSeq)
	binary.BigEndian.PutUint64(b[8:], e.Sent)
	binary.BigEndian.PutUint32(b[16:], uint32(e.ErrorCode))
	return b
}

// ---------------------------------------------------------------- credits

// CreditLedger implements the cumulative ACK rules on both ends. HELLO grants
// nothing; the first ACK must acknowledge seq 0; cumulative_credit_total is
// monotonic and never exceeds ack_seq + capacity; a duplicate identical ACK
// is idempotent.
type CreditLedger struct {
	capacity      uint64
	acked         bool
	ackSeq, total uint64
	spent, sent   uint64
}

func NewCreditLedger(capacity uint64) *CreditLedger { return &CreditLedger{capacity: capacity} }

func (l *CreditLedger) Total() uint64  { return l.total }
func (l *CreditLedger) Spent() uint64  { return l.spent }
func (l *CreditLedger) Sent() uint64   { return l.sent }
func (l *CreditLedger) AckSeq() uint64 { return l.ackSeq }
func (l *CreditLedger) CanSpend() bool { return l.spent < l.total }

// ApplyAck validates and applies one ACK; on error the ledger is unchanged.
func (l *CreditLedger) ApplyAck(ackSeq, total uint64) error {
	if l.capacity == 0 || l.capacity > InitialCreditFrames {
		return streamErr(StreamCredit, "capacity %d", l.capacity)
	}
	if !l.acked && ackSeq != 0 {
		return streamErr(StreamCredit, "first ACK acks seq %d", ackSeq)
	}
	if l.acked && ackSeq == l.ackSeq && total == l.total {
		return nil
	}
	if ackSeq > math.MaxUint64-l.capacity {
		return streamErr(StreamCredit, "overflow")
	}
	switch {
	case l.acked && ackSeq < l.ackSeq:
		return streamErr(StreamCredit, "ack regression %d<%d", ackSeq, l.ackSeq)
	case ackSeq > l.sent:
		return streamErr(StreamCredit, "ack of unsent seq %d (sent %d)", ackSeq, l.sent)
	case l.acked && total < l.total:
		return streamErr(StreamCredit, "credit regression %d<%d", total, l.total)
	case total > ackSeq+l.capacity:
		return streamErr(StreamCredit, "credit inflation %d>%d", total, ackSeq+l.capacity)
	}
	l.acked, l.ackSeq, l.total = true, ackSeq, total
	return nil
}

// Spend reserves one credit before a capture.
func (l *CreditLedger) Spend() error {
	if !l.CanSpend() {
		return streamErr(StreamCredit, "no credit")
	}
	l.spent++
	return nil
}

// MarkSent records a completely emitted (sender) or admitted (host) frame.
// On the host, where nothing calls Spend, it also checks the frame had credit.
func (l *CreditLedger) MarkSent(seq uint64) error {
	if seq != l.sent+1 {
		return streamErr(StreamSeq, "seq %d after %d", seq, l.sent)
	}
	if seq > l.total {
		return streamErr(StreamCredit, "frame %d without credit (total %d)", seq, l.total)
	}
	l.sent = seq
	l.spent = max(l.spent, seq)
	return nil
}

// HostControl encodes host control records and refuses invalid ACKs.
type HostControl struct {
	binding SeatBinding
	ledger  *CreditLedger
}

func NewHostControl(b SeatBinding) *HostControl {
	return &HostControl{binding: b, ledger: NewCreditLedger(b.MaxInflightFrames)}
}

// Ledger is the host credit ledger; pass it to NewStreamDecoder.
func (h *HostControl) Ledger() *CreditLedger { return h.ledger }

// Hello carries the canonical binding JSON; it grants no credit.
func (h *HostControl) Hello() []byte {
	return EncodeControl(MagicControl, KindHello, h.binding.SHA256, h.binding.Canonical)
}

// Ack applies the ACK to the host ledger and encodes it only if valid.
func (h *HostControl) Ack(ackSeq, total uint64) ([]byte, error) {
	if err := h.ledger.ApplyAck(ackSeq, total); err != nil {
		return nil, err
	}
	return EncodeControl(MagicControl, KindAck, h.binding.SHA256, EncodeAckBody(ackSeq, total)), nil
}

func (h *HostControl) Stop() []byte {
	return EncodeControl(MagicControl, KindStop, h.binding.SHA256, nil)
}

// ---------------------------------------------------------------- frames

// FrameHeader is the 128-byte W8UF header without its constant fields.
type FrameHeader struct {
	RecordBytes, Seq, Tick, CaptureMonoNS uint64
	Width, Height, Stride                 uint32
	PixelFormat, Flags                    uint16
	PayloadBytes                          uint64
	SHA256RGB24, BindingSHA256            [32]byte
}

func (h FrameHeader) Encode() [FrameHeaderBytes]byte {
	var b [FrameHeaderBytes]byte
	copy(b[:], MagicFrame)
	binary.BigEndian.PutUint16(b[4:], WireVersion)
	binary.BigEndian.PutUint16(b[6:], FrameHeaderBytes)
	binary.BigEndian.PutUint64(b[8:], h.RecordBytes)
	binary.BigEndian.PutUint64(b[16:], h.Seq)
	binary.BigEndian.PutUint64(b[24:], h.Tick)
	binary.BigEndian.PutUint64(b[32:], h.CaptureMonoNS)
	binary.BigEndian.PutUint32(b[40:], h.Width)
	binary.BigEndian.PutUint32(b[44:], h.Height)
	binary.BigEndian.PutUint32(b[48:], h.Stride)
	binary.BigEndian.PutUint16(b[52:], h.PixelFormat)
	binary.BigEndian.PutUint16(b[54:], h.Flags)
	binary.BigEndian.PutUint64(b[56:], h.PayloadBytes)
	copy(b[64:], h.SHA256RGB24[:])
	copy(b[96:], h.BindingSHA256[:])
	return b
}

func parseFrameHeader(b []byte) FrameHeader {
	var h FrameHeader
	h.RecordBytes = binary.BigEndian.Uint64(b[8:])
	h.Seq = binary.BigEndian.Uint64(b[16:])
	h.Tick = binary.BigEndian.Uint64(b[24:])
	h.CaptureMonoNS = binary.BigEndian.Uint64(b[32:])
	h.Width = binary.BigEndian.Uint32(b[40:])
	h.Height = binary.BigEndian.Uint32(b[44:])
	h.Stride = binary.BigEndian.Uint32(b[48:])
	h.PixelFormat = binary.BigEndian.Uint16(b[52:])
	h.Flags = binary.BigEndian.Uint16(b[54:])
	h.PayloadBytes = binary.BigEndian.Uint64(b[56:])
	copy(h.SHA256RGB24[:], b[64:96])
	copy(h.BindingSHA256[:], b[96:128])
	return h
}

// PairLegs validates both XWD legs against the binding geometry and checks
// that their canonical RGB24 digests agree. It returns leg A's RGB24.
func PairLegs(a, b []byte, w, h int) ([]byte, [32]byte, error) {
	var zero [32]byte
	hA, err := validateLeg(a, w, h)
	if err != nil {
		return nil, zero, err
	}
	hB, err := validateLeg(b, w, h)
	if err != nil {
		return nil, zero, err
	}
	rgb, err := RGB24(a, hA)
	if err != nil {
		return nil, zero, streamErr(StreamWire, "leg A: %v", err)
	}
	shaA := sha256.Sum256(rgb)
	shaB, err := FrameSHA256(b, hB)
	if err != nil {
		return nil, zero, streamErr(StreamWire, "leg B: %v", err)
	}
	if shaA != shaB {
		return nil, zero, streamErr(StreamTorn, "canonical legs differ")
	}
	return rgb, shaA, nil
}

func validateLeg(leg []byte, w, h int) (Header, error) {
	hd, err := Validate(leg, w, h)
	if err != nil {
		if err.Error() == "E_XWD_GEOMETRY" {
			return hd, streamErr(StreamGeometry, "leg %v", err)
		}
		return hd, streamErr(StreamWire, "leg %v", err)
	}
	return hd, nil
}

// EncodeFrame builds one complete FRAME record from validated legs.
func EncodeFrame(seq, tick, mono uint64, flags uint16, sha, binding [32]byte, w, h int, a, b []byte) []byte {
	payload := uint64(16 + len(a) + len(b))
	hdr := FrameHeader{RecordBytes: FrameHeaderBytes + payload, Seq: seq, Tick: tick, CaptureMonoNS: mono, Width: uint32(w), Height: uint32(h), Stride: uint32(3 * w), PixelFormat: PixelFormatRGB24, Flags: flags, PayloadBytes: payload, SHA256RGB24: sha, BindingSHA256: binding}.Encode()
	out := make([]byte, 0, FrameHeaderBytes+payload)
	out = append(out, hdr[:]...)
	out = binary.BigEndian.AppendUint64(out, uint64(len(a)))
	out = append(out, a...)
	out = binary.BigEndian.AppendUint64(out, uint64(len(b)))
	return append(out, b...)
}

// ---------------------------------------------------------------- decoder

// StreamRecord is one decoded downstream record.
type StreamRecord struct {
	Magic string // MagicHelloConfirm, MagicFrame or MagicEnd
	Frame *FrameRecord
	End   *EndBody
}

// FrameRecord is an admitted pair: both legs valid, equal and hash-checked.
type FrameRecord struct {
	Header     FrameHeader
	LegA, LegB []byte
	RGB24      []byte
}

// StreamDecoder decodes one stream incarnation. Any error is terminal: there
// is no magic-byte resynchronisation.
type StreamDecoder struct {
	r       io.Reader
	b       SeatBinding
	ledger  *CreditLedger
	state   int // 0 unbound, 1 bound, 2 ended
	lastSeq uint64
	err     error
}

// NewStreamDecoder decodes against binding b. A non-nil ledger (normally
// HostControl.Ledger()) additionally refuses frames sent without credit.
func NewStreamDecoder(r io.Reader, b SeatBinding, ledger *CreditLedger) *StreamDecoder {
	return &StreamDecoder{r: r, b: b, ledger: ledger}
}

// LastSeq is the last admitted frame sequence number.
func (d *StreamDecoder) LastSeq() uint64 { return d.lastSeq }

func (d *StreamDecoder) Next() (StreamRecord, error) {
	if d.err != nil {
		return StreamRecord{}, d.err
	}
	rec, err := d.next()
	if err != nil {
		d.err = err
	}
	return rec, err
}

func (d *StreamDecoder) next() (StreamRecord, error) {
	if d.state == 2 {
		return StreamRecord{}, io.EOF
	}
	var hdr [FrameHeaderBytes]byte
	if n, err := io.ReadFull(d.r, hdr[:4]); err != nil {
		if n == 0 {
			return StreamRecord{}, streamErr(StreamWire, "EOF before END")
		}
		return StreamRecord{}, streamErr(StreamWire, "truncated magic")
	}
	switch string(hdr[:4]) {
	case MagicHelloConfirm:
		if d.state != 0 {
			return StreamRecord{}, streamErr(StreamWire, "repeated HELLO confirmation")
		}
		var ch [ControlHeaderBytes]byte
		copy(ch[:], hdr[:4])
		c, err := readControlRest(d.r, ch, MagicHelloConfirm)
		if err != nil {
			return StreamRecord{}, err
		}
		if c.BindingSHA != d.b.SHA256 {
			return StreamRecord{}, streamErr(StreamBinding, "HELLO confirmation digest")
		}
		if d.b.TickMode != TickModeUnavailable {
			return StreamRecord{}, streamErr(StreamTick, "tick_mode %s unsupported", d.b.TickMode)
		}
		d.state = 1
		return StreamRecord{Magic: MagicHelloConfirm}, nil
	case MagicEnd:
		var ch [ControlHeaderBytes]byte
		copy(ch[:], hdr[:4])
		c, err := readControlRest(d.r, ch, MagicEnd)
		if err != nil {
			return StreamRecord{}, err
		}
		if c.BindingSHA != d.b.SHA256 && (d.state != 0 || c.BindingSHA != [32]byte{}) {
			return StreamRecord{}, streamErr(StreamBinding, "END digest")
		}
		e := EndBody{LastSeq: binary.BigEndian.Uint64(c.Body), Sent: binary.BigEndian.Uint64(c.Body[8:]), ErrorCode: StreamCode(binary.BigEndian.Uint32(c.Body[16:]))}
		if StreamCodeName(e.ErrorCode) == "" {
			return StreamRecord{}, streamErr(StreamWire, "END error_code %d", e.ErrorCode)
		}
		if e.LastSeq != d.lastSeq || e.Sent != d.lastSeq {
			return StreamRecord{}, streamErr(StreamSeq, "END last_seq %d sent %d, admitted %d", e.LastSeq, e.Sent, d.lastSeq)
		}
		var one [1]byte
		if n, _ := io.ReadFull(d.r, one[:]); n != 0 {
			return StreamRecord{}, streamErr(StreamLength, "bytes after END")
		}
		d.state = 2
		return StreamRecord{Magic: MagicEnd, End: &e}, nil
	case MagicFrame:
		if d.state != 1 {
			return StreamRecord{}, streamErr(StreamWire, "FRAME before HELLO confirmation")
		}
		f, err := d.frame(hdr)
		if err != nil {
			return StreamRecord{}, err
		}
		return StreamRecord{Magic: MagicFrame, Frame: f}, nil
	}
	return StreamRecord{}, streamErr(StreamWire, "magic %q", hdr[:4])
}

func (d *StreamDecoder) frame(hdr [FrameHeaderBytes]byte) (*FrameRecord, error) {
	if _, err := io.ReadFull(d.r, hdr[4:]); err != nil {
		return nil, streamErr(StreamWire, "truncated FRAME header")
	}
	if v := binary.BigEndian.Uint16(hdr[4:]); v != WireVersion {
		return nil, streamErr(StreamWire, "version %d", v)
	}
	if n := binary.BigEndian.Uint16(hdr[6:]); n != FrameHeaderBytes {
		return nil, streamErr(StreamWire, "header_bytes %d", n)
	}
	h := parseFrameHeader(hdr[:])
	w, ht := d.b.Width, d.b.Height
	switch {
	case h.BindingSHA256 != d.b.SHA256:
		return nil, streamErr(StreamBinding, "FRAME binding digest")
	case h.Flags&^knownFlags != 0:
		return nil, streamErr(StreamWire, "unknown flags %#x", h.Flags)
	case h.Flags&FlagPairUnderGrab == 0:
		return nil, streamErr(StreamWire, "pair_under_grab unset")
	case h.PixelFormat != PixelFormatRGB24:
		return nil, streamErr(StreamWire, "pixel_format %d", h.PixelFormat)
	case h.Width != uint32(w) || h.Height != uint32(ht):
		return nil, streamErr(StreamGeometry, "header %dx%d, binding %dx%d", h.Width, h.Height, w, ht)
	case h.Stride != uint32(3*w):
		return nil, streamErr(StreamGeometry, "stride %d", h.Stride)
	case h.RecordBytes > d.b.RecordLimit():
		return nil, streamErr(StreamLength, "record_bytes %d > cap %d", h.RecordBytes, d.b.RecordLimit())
	case h.RecordBytes < FrameHeaderBytes+16 || h.PayloadBytes != h.RecordBytes-FrameHeaderBytes:
		return nil, streamErr(StreamLength, "record_bytes %d payload_bytes %d", h.RecordBytes, h.PayloadBytes)
	case h.Seq != d.lastSeq+1:
		return nil, streamErr(StreamSeq, "seq %d after %d", h.Seq, d.lastSeq)
	}
	tickValid := h.Flags&FlagTickValid != 0
	if d.b.TickMode != TickModeCommitted && (tickValid || h.Tick != 0) {
		return nil, streamErr(StreamTick, "tick %d valid=%v with tick_mode %s", h.Tick, tickValid, d.b.TickMode)
	}
	if d.b.TickMode == TickModeCommitted && !tickValid {
		return nil, streamErr(StreamTick, "committed tick missing")
	}
	a, err := d.leg(h.PayloadBytes-16, w, ht)
	if err != nil {
		return nil, err
	}
	rest := h.PayloadBytes - 16 - uint64(len(a))
	b, err := d.leg(rest, w, ht)
	if err != nil {
		return nil, err
	}
	if uint64(len(b)) != rest {
		return nil, streamErr(StreamLength, "lenB %d, payload leaves %d", len(b), rest)
	}
	rgb, sha, err := PairLegs(a, b, w, ht)
	if err != nil {
		return nil, err
	}
	if sha != h.SHA256RGB24 {
		return nil, streamErr(StreamHash, "header digest differs from canonical RGB24")
	}
	if d.ledger != nil {
		if err := d.ledger.MarkSent(h.Seq); err != nil {
			return nil, err
		}
	}
	d.lastSeq = h.Seq
	return &FrameRecord{Header: h, LegA: a, LegB: b, RGB24: rgb}, nil
}

// leg reads one u64 length word and its leg, bounded by room and the leg cap.
func (d *StreamDecoder) leg(room uint64, w, h int) ([]byte, error) {
	var lw [8]byte
	if _, err := io.ReadFull(d.r, lw[:]); err != nil {
		return nil, streamErr(StreamLength, "truncated leg length")
	}
	n := binary.BigEndian.Uint64(lw[:])
	if n > room || n > legCap(w, h) || n < 100 {
		return nil, streamErr(StreamLength, "leg length %d (room %d)", n, room)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(d.r, buf); err != nil {
		return nil, streamErr(StreamLength, "truncated leg")
	}
	return buf, nil
}
