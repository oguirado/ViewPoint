package ramcap

// Cross-language golden vectors for the W8U resident pair stream (wire v1).
//
// TestSeatstreamVectors regenerates every vector in memory from the real
// encoders (CanonicalJSON, HostControl, EncodeControl, EncodeFrame, PairLegs)
// and asserts byte equality with testdata/w8u/seatstream_vectors.json; every
// bad case is replayed through the real decoder (sender->host) or through
// vecSenderControl, which applies the ramcap control rules exactly as
// cmd/srf-fbread/stream.go does (host->sender). Regenerate with
//
//	go test ./src/ramcap/ -run SeatstreamVectors -update
//
// Without -update the test never writes. Every value is fixed: no clock, no
// randomness.

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"
	"testing"
	"testing/iotest"
)

var updateSeatstreamVectors = flag.Bool("update", false, "rewrite testdata/w8u/seatstream_vectors.json")

const (
	vecSchema = "os.cu.w8u-seatstream-vectors/v1"
	vecW      = 4
	vecH      = 3
	vecXID    = 0x200001
	vecRunID  = "w8u-vec-0001"
	vecMono   = 1000000123
	vecPath   = "../../testdata/w8u/seatstream_vectors.json"

	dirSenderToHost = "sender->host"
	dirHostToSender = "host->sender"
)

func vecDigest(label string) string {
	s := sha256.Sum256([]byte("w8u-vectors/" + label))
	return hex.EncodeToString(s[:])
}

// vecLeg is a frozen copy of the synthetic XWD builder (header fields chosen
// independently of the canonical RGB24 pixels), so the vectors do not move
// when other tests change their helpers.
func vecLeg(w, h int, order uint32, name string, rgb []byte) []byte {
	nm := append([]byte(name), 0)
	fields := []uint32{uint32(100 + len(nm)), 7, 2, 24, uint32(w), uint32(h), 0, order, 32, order, 32, 32, uint32(4 * w), 4, 0xff0000, 0xff00, 0xff, 8, 0, 0, uint32(w), uint32(h), 0, 0, 0}
	out := make([]byte, 100+len(nm)+4*w*h)
	for i, v := range fields {
		binary.BigEndian.PutUint32(out[4*i:], v)
	}
	copy(out[100:], nm)
	px := out[100+len(nm):]
	for i := 0; i < w*h; i++ {
		r, g, b := rgb[3*i], rgb[3*i+1], rgb[3*i+2]
		if order == 0 {
			px[4*i+2], px[4*i+1], px[4*i] = r, g, b
		} else {
			px[4*i+1], px[4*i+2], px[4*i+3] = r, g, b
		}
	}
	return out
}

func vecRGB(w, h int, salt byte) []byte {
	rgb := make([]byte, 3*w*h)
	for i := range rgb {
		rgb[i] = byte(i*37) + salt
	}
	return rgb
}

func vecBindingFields(tickMode string) map[string]any {
	return map[string]any{
		"schema":                "w8u-binding/1",
		"run_id":                vecRunID,
		"lease_id":              "lease-w8u-vec-0001",
		"seat_grant_sha256":     vecDigest("seat_grant"),
		"container_id":          vecDigest("container"),
		"target_handle":         map[string]any{"kind": "capsule", "canonical_id": "os-cu-0/app", "generation": 1},
		"epoch":                 3,
		"display":               ":100",
		"xid":                   uint64(vecXID),
		"width":                 vecW,
		"height":                vecH,
		"frames_root":           StreamFramesRoot,
		"frame_run_root":        StreamRunParent + "/" + vecRunID,
		"image_sha256":          vecDigest("image"),
		"reader_sha256":         vecDigest("reader"),
		"exec_allowlist_sha256": vecDigest("exec_allowlist"),
		"max_record_bytes":      RecordCap(vecW, vecH),
		"max_inflight_frames":   InitialCreditFrames,
		"tick_mode":             tickMode,
	}
}

func vecBinding(t *testing.T, tickMode string) SeatBinding {
	t.Helper()
	raw, err := CanonicalJSON(vecBindingFields(tickMode))
	if err != nil {
		t.Fatal(err)
	}
	b, err := ParseSeatBinding(raw)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// vecSenderControl replays a host->sender control stream through the sender
// rules of cmd/srf-fbread/stream.go (hello + control), using the real ramcap
// ReadControl, ParseSeatBinding, DecodeAckBody and CreditLedger. Schedule
// model: before reading each bound-state control the sender spends every
// available credit on one frame each (Spend + MarkSent), which is one valid
// interleaving of the real sender. nil means a clean STOP.
func vecSenderControl(stream []byte, xid uint32, display string) error {
	r := bytes.NewReader(stream)
	closed := func(err error) error {
		if errors.Is(err, io.EOF) {
			return streamErr(StreamWire, "control channel closed without STOP")
		}
		return err
	}
	rec, err := ReadControl(r, MagicControl)
	if err != nil {
		return closed(err)
	}
	if rec.Kind != KindHello {
		return streamErr(StreamWire, "first control is not HELLO")
	}
	if sha256.Sum256(rec.Body) != rec.BindingSHA {
		return streamErr(StreamBinding, "HELLO digest")
	}
	b, err := ParseSeatBinding(rec.Body)
	if err != nil {
		return err
	}
	if b.XID != xid || b.Display != display {
		return streamErr(StreamBinding, "display/xid differ from reader argv")
	}
	if b.TickMode != TickModeUnavailable {
		return streamErr(StreamTick, "tick_mode committed unsupported")
	}
	l := NewCreditLedger(b.MaxInflightFrames)
	for {
		for l.CanSpend() {
			if err := l.Spend(); err != nil {
				return err
			}
			if err := l.MarkSent(l.Sent() + 1); err != nil {
				return err
			}
		}
		rec, err := ReadControl(r, MagicControl)
		if err != nil {
			return closed(err)
		}
		switch rec.Kind {
		case KindAck:
			if rec.BindingSHA != b.SHA256 {
				return streamErr(StreamCredit, "ACK binding digest")
			}
			ackSeq, total, err := DecodeAckBody(rec.Body)
			if err != nil {
				return err
			}
			if err := l.ApplyAck(ackSeq, total); err != nil {
				return err
			}
		case KindStop:
			if rec.BindingSHA != b.SHA256 {
				return streamErr(StreamBinding, "STOP binding digest")
			}
			return nil
		default:
			return streamErr(StreamWire, "HELLO after bind")
		}
	}
}

func vecDecodeAll(wire []byte, b SeatBinding) ([]StreamRecord, error) {
	d := NewStreamDecoder(iotest.HalfReader(bytes.NewReader(wire)), b, nil)
	var recs []StreamRecord
	for {
		r, err := d.Next()
		if err == io.EOF {
			return recs, nil
		}
		if err != nil {
			return recs, err
		}
		recs = append(recs, r)
	}
}

func cat(parts ...[]byte) []byte { return bytes.Join(parts, nil) }

func hx(b []byte) string { return hex.EncodeToString(b) }

// reframe re-encodes the header of a complete FRAME record after mutation and
// keeps its payload unchanged.
func reframe(rec []byte, mut func(*FrameHeader)) []byte {
	h := parseFrameHeader(rec[:FrameHeaderBytes])
	mut(&h)
	e := h.Encode()
	return cat(e[:], rec[FrameHeaderBytes:])
}

type vecBad struct {
	name, dir, note string
	wire            []byte
	want            StreamCode
}

func buildSeatstreamVectors(t *testing.T) []byte {
	t.Helper()
	bd := vecBinding(t, TickModeUnavailable)
	host := NewHostControl(bd)

	// Good records.
	hello := host.Hello()
	ack0, err := host.Ack(0, InitialCreditFrames)
	if err != nil {
		t.Fatal(err)
	}
	for seq := uint64(1); seq <= 2; seq++ {
		if err := host.Ledger().MarkSent(seq); err != nil {
			t.Fatal(err)
		}
	}
	ackLater, err := host.Ack(2, 2+InitialCreditFrames)
	if err != nil {
		t.Fatal(err)
	}
	stop := host.Stop()
	confirm := EncodeControl(MagicHelloConfirm, KindHello, bd.SHA256, nil)

	rgb := vecRGB(vecW, vecH, 5)
	legA := vecLeg(vecW, vecH, 0, "Xvfb fb", rgb)
	legB := vecLeg(vecW, vecH, 1, "srf-fbread xid=0x200001", rgb)
	if bytes.Equal(legA[:FrameHeaderBytes], legB[:FrameHeaderBytes]) {
		t.Fatal("legs must have distinct XWD headers")
	}
	gotRGB, sha, err := PairLegs(legA, legB, vecW, vecH)
	if err != nil || !bytes.Equal(gotRGB, rgb) {
		t.Fatalf("pair legs: %v", err)
	}
	frame1 := EncodeFrame(1, 0, vecMono, FlagPairUnderGrab, sha, bd.SHA256, vecW, vecH, legA, legB)
	frame2 := EncodeFrame(2, 0, vecMono+1000, FlagPairUnderGrab, sha, bd.SHA256, vecW, vecH, legA, legB)
	end1 := EncodeControl(MagicEnd, KindEnd, bd.SHA256, EndBody{LastSeq: 1, Sent: 1}.Encode())
	end2 := EncodeControl(MagicEnd, KindEnd, bd.SHA256, EndBody{LastSeq: 2, Sent: 2}.Encode())
	endUnbound := EncodeControl(MagicEnd, KindEnd, [32]byte{}, EndBody{ErrorCode: StreamBinding}.Encode())

	// Good streams must pass the real decoder / sender rules.
	downGood := cat(confirm, frame1, end1)
	recs, err := vecDecodeAll(downGood, bd)
	if err != nil || len(recs) != 3 || recs[1].Frame == nil || !bytes.Equal(recs[1].Frame.RGB24, rgb) || recs[2].End.ErrorCode != StreamOK {
		t.Fatalf("good downstream: %v %d", err, len(recs))
	}
	if recs, err := vecDecodeAll(endUnbound, bd); err != nil || len(recs) != 1 || recs[0].End.ErrorCode != StreamBinding {
		t.Fatalf("END before HELLO: %v", err)
	}
	upGood := cat(hello, ack0, ackLater, stop)
	if err := vecSenderControl(upGood, bd.XID, bd.Display); err != nil {
		t.Fatalf("good upstream: %v", err)
	}

	// Bad cases.
	torn := vecLeg(vecW, vecH, 1, "srf-fbread xid=0x200001", vecRGB(vecW, vecH, 6))
	tornFrame := EncodeFrame(1, 0, vecMono, FlagPairUnderGrab, sha, bd.SHA256, vecW, vecH, legA, torn)
	rgbWide := vecRGB(vecW+1, vecH, 5)
	wideA, wideB := vecLeg(vecW+1, vecH, 0, "Xvfb fb", rgbWide), vecLeg(vecW+1, vecH, 1, "srf-fbread xid=0x200001", rgbWide)
	shaWide := sha256.Sum256(rgbWide)
	geomFrame := EncodeFrame(1, 0, vecMono, FlagPairUnderGrab, shaWide, bd.SHA256, vecW, vecH, wideA, wideB)
	badHelloDigest := cat(hello[:ControlHeaderBytes], hello[ControlHeaderBytes:])
	badHelloDigest[12+31] ^= 1
	committed := vecBinding(t, TickModeCommitted)
	committedHello := NewHostControl(committed).Hello()
	badConfirm := EncodeControl(MagicHelloConfirm, KindHello, committed.SHA256, nil)
	ackInflate := EncodeControl(MagicControl, KindAck, bd.SHA256, EncodeAckBody(0, InitialCreditFrames+1))
	ackRegress := EncodeControl(MagicControl, KindAck, bd.SHA256, EncodeAckBody(1, 2+InitialCreditFrames))
	bad := []vecBad{
		{"wire_truncated_frame_header", dirSenderToHost, "FRAME fixed header cut at 60 of 128 bytes", cat(confirm, frame1[:60]), StreamWire},
		{"wire_unknown_flags_bit", dirSenderToHost, "flags bit5 set", cat(confirm, reframe(frame1, func(h *FrameHeader) { h.Flags |= 1 << 5 }), end1), StreamWire},
		{"wire_unknown_pixel_format", dirSenderToHost, "pixel_format 2", cat(confirm, reframe(frame1, func(h *FrameHeader) { h.PixelFormat = 2 }), end1), StreamWire},
		{"wire_truncated_control_header", dirHostToSender, "HELLO control header cut at 20 of 44 bytes", hello[:20], StreamWire},
		{"length_truncated_payload", dirSenderToHost, "FRAME payload cut 40 bytes after the header", cat(confirm, frame1[:FrameHeaderBytes+40]), StreamLength},
		{"length_trailing_byte_after_end", dirSenderToHost, "one 0x00 byte after W8UE", cat(confirm, frame1, end1, []byte{0}), StreamLength},
		{"length_record_above_cap", dirSenderToHost, "record_bytes = RecordCap(4,3)+1, payload_bytes consistent", cat(confirm, reframe(frame1, func(h *FrameHeader) {
			h.RecordBytes = RecordCap(vecW, vecH) + 1
			h.PayloadBytes = h.RecordBytes - FrameHeaderBytes
		}), end1), StreamLength},
		{"length_truncated_hello_body", dirHostToSender, "HELLO body cut 10 bytes short", hello[:len(hello)-10], StreamLength},
		{"seq_duplicate", dirSenderToHost, "second FRAME repeats seq 1", cat(confirm, frame1, reframe(frame2, func(h *FrameHeader) { h.Seq = 1 }), end2), StreamSeq},
		{"hash_forged_sha", dirSenderToHost, "sha256_rgb24 byte 0 xor 1", cat(confirm, reframe(frame1, func(h *FrameHeader) { h.SHA256RGB24[0] ^= 1 }), end1), StreamHash},
		{"binding_wrong_hello_digest", dirHostToSender, "HELLO header binding_sha256 byte 31 xor 1", badHelloDigest, StreamBinding},
		{"binding_wrong_confirm_digest", dirSenderToHost, "W8UH carries the digest of a different binding", cat(badConfirm, end1), StreamBinding},
		{"credit_ack_inflation", dirHostToSender, "first ACK grants total 3 > ack_seq 0 + capacity 2", cat(hello, ackInflate, stop), StreamCredit},
		{"credit_ack_regression", dirHostToSender, "ACK ack_seq 1 after ack_seq 2", cat(hello, ack0, ackLater, ackRegress, stop), StreamCredit},
		{"tick_committed_mode", dirHostToSender, "HELLO binding has tick_mode committed (unsupported in v1)", cat(committedHello, ack0, stop), StreamTick},
		{"tick_valid_without_committed", dirSenderToHost, "FRAME flags tick_valid, tick 5, binding tick_mode unavailable", cat(confirm, reframe(frame1, func(h *FrameHeader) { h.Flags |= FlagTickValid; h.Tick = 5 }), end1), StreamTick},
		{"geometry_leg_mismatch", dirSenderToHost, "header 4x3 matches binding, both XWD legs are 5x3", cat(confirm, geomFrame, end1), StreamGeometry},
		{"torn_legs_differ", dirSenderToHost, "leg B canonical RGB24 differs from leg A", cat(confirm, tornFrame, end1), StreamTorn},
	}
	var badOut []any
	for _, c := range bad {
		var err error
		switch c.dir {
		case dirSenderToHost:
			_, err = vecDecodeAll(c.wire, bd)
		case dirHostToSender:
			err = vecSenderControl(c.wire, bd.XID, bd.Display)
		}
		if got := StreamCodeOf(err); err == nil || got != c.want {
			t.Fatalf("%s: got %v, want %s", c.name, err, StreamCodeName(c.want))
		}
		badOut = append(badOut, map[string]any{
			"name": c.name, "direction": c.dir, "note": c.note, "hex": hx(c.wire),
			"error_name": StreamCodeName(c.want), "error_code": uint32(c.want),
		})
	}

	codes := map[string]any{}
	var table []any
	for _, e := range StreamErrorTable {
		codes[e.Name] = uint32(e.Code)
		table = append(table, map[string]any{"code": uint32(e.Code), "name": e.Name, "meaning": e.Meaning})
	}

	rec := func(dir, magic string, kind int, b []byte) map[string]any {
		return map[string]any{"direction": dir, "magic": magic, "kind": kind, "bytes": len(b), "hex": hx(b)}
	}
	ackRec := func(b []byte, ackSeq, total uint64) map[string]any {
		m := rec(dirHostToSender, MagicControl, KindAck, b)
		m["ack_seq"], m["cumulative_credit_total"] = ackSeq, total
		return m
	}
	endRec := func(b []byte, e EndBody, zero bool) map[string]any {
		m := rec(dirSenderToHost, MagicEnd, KindEnd, b)
		m["last_seq"], m["sent"], m["error_code"], m["error_name"], m["binding_digest_zero"] = e.LastSeq, e.Sent, uint32(e.ErrorCode), StreamCodeName(e.ErrorCode), zero
		return m
	}
	frameRec := rec(dirSenderToHost, MagicFrame, 0, frame1)
	delete(frameRec, "kind")
	for k, v := range map[string]any{
		"seq": 1, "tick": 0, "capture_mono_ns": uint64(vecMono), "width": vecW, "height": vecH, "stride": 3 * vecW,
		"pixel_format": PixelFormatRGB24, "flags": FlagPairUnderGrab, "record_bytes": len(frame1), "payload_bytes": len(frame1) - FrameHeaderBytes,
		"sha256_rgb24": hx(sha[:]), "rgb24_hex": hx(rgb), "leg_a_hex": hx(legA), "leg_b_hex": hx(legB),
	} {
		frameRec[k] = v
	}

	doc := map[string]any{
		"schema":       vecSchema,
		"wire_version": WireVersion,
		"generator":    "src/ramcap/seatstream_vectors_test.go (go test ./src/ramcap/ -run SeatstreamVectors -update)",
		"conventions": map[string]any{
			"hex":            "lowercase, no separators",
			"scalars":        "unsigned big endian",
			"bad_hex":        "complete byte stream consumed from its start by the decoder (sender->host) or the sender control reader (host->sender); the first error is terminal",
			"sender_model":   "host->sender cases run the cmd/srf-fbread sender control rules; before each bound-state control the sender spends every available credit on one frame",
			"host_decoder":   "sender->host cases decode against binding with no host credit ledger attached",
			"binding_target": "decoding always uses binding.canonical_utf8; tick_committed_mode sends, and binding_wrong_confirm_digest confirms, binding.committed_canonical_utf8 (same fields, tick_mode committed)",
		},
		"binding": map[string]any{
			"fields":                   vecBindingFields(TickModeUnavailable),
			"canonical_utf8":           string(bd.Canonical),
			"canonical_hex":            hx(bd.Canonical),
			"sha256":                   hx(bd.SHA256[:]),
			"record_cap":               RecordCap(vecW, vecH),
			"record_limit":             bd.RecordLimit(),
			"committed_canonical_utf8": string(committed.Canonical),
			"committed_sha256":         hx(committed.SHA256[:]),
		},
		"good": map[string]any{
			"host_hello":         rec(dirHostToSender, MagicControl, KindHello, hello),
			"hello_confirm":      rec(dirSenderToHost, MagicHelloConfirm, KindHello, confirm),
			"frame_seq1":         frameRec,
			"ack_first":          ackRec(ack0, 0, InitialCreditFrames),
			"ack_later":          ackRec(ackLater, 2, 2+InitialCreditFrames),
			"stop":               rec(dirHostToSender, MagicControl, KindStop, stop),
			"end_normal":         endRec(end1, EndBody{LastSeq: 1, Sent: 1}, false),
			"end_before_hello":   endRec(endUnbound, EndBody{ErrorCode: StreamBinding}, true),
			"stream_sender_host": map[string]any{"direction": dirSenderToHost, "records": []any{"hello_confirm", "frame_seq1", "end_normal"}, "hex": hx(downGood)},
			"stream_host_sender": map[string]any{"direction": dirHostToSender, "records": []any{"host_hello", "ack_first", "ack_later", "stop"}, "hex": hx(upGood)},
		},
		"bad":         badOut,
		"error_codes": codes,
		"error_table": table,
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", " ")
	if err := enc.Encode(doc); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestSeatstreamVectors(t *testing.T) {
	got := buildSeatstreamVectors(t)
	if again := buildSeatstreamVectors(t); !bytes.Equal(got, again) {
		t.Fatal("vector generation is not deterministic")
	}
	if *updateSeatstreamVectors {
		if err := os.MkdirAll(filepath.Dir(vecPath), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(vecPath, got, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s (%d bytes)", vecPath, len(got))
		return
	}
	want, err := os.ReadFile(vecPath)
	if err != nil {
		t.Fatalf("%v (run with -update to create it)", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("%s differs from the regenerated vectors (%d vs %d bytes); rerun with -update only if the wire change is intended", vecPath, len(want), len(got))
	}
	// Replay the committed bad cases from the file itself (as the Python peer will).
	var file struct {
		Bad []map[string]any `json:"bad"`
	}
	if err := json.Unmarshal(want, &file); err != nil {
		t.Fatal(err)
	}
	bd := vecBinding(t, TickModeUnavailable)
	for _, c := range file.Bad {
		wire, err := hex.DecodeString(c["hex"].(string))
		if err != nil {
			t.Fatal(err)
		}
		var derr error
		if c["direction"] == dirSenderToHost {
			_, derr = vecDecodeAll(wire, bd)
		} else {
			derr = vecSenderControl(wire, bd.XID, bd.Display)
		}
		code := StreamCodeOf(derr)
		if derr == nil || StreamCodeName(code) != c["error_name"] || float64(code) != c["error_code"] {
			t.Fatalf("%v: got %v, want %v/%v", c["name"], derr, c["error_name"], c["error_code"])
		}
	}
}
