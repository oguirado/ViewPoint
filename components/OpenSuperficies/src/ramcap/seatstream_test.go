package ramcap

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"io"
	"math"
	"runtime"
	"strings"
	"testing"
	"testing/iotest"
)

// synthLeg builds a little XWD whose header (name, byte order) is chosen
// independently of its canonical RGB24 pixels.
func synthLeg(w, h int, order uint32, name string, rgb []byte) []byte {
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

func synthRGB(w, h int, salt byte) []byte {
	rgb := make([]byte, 3*w*h)
	for i := range rgb {
		rgb[i] = byte(i*7) + salt
	}
	return rgb
}

func bindingMap(w, h int) map[string]any {
	return map[string]any{
		"schema": "w8u-binding/1", "run_id": "run-1", "lease_id": "lease-1", "seat_grant_sha256": strings.Repeat("a", 64),
		"container_id": "c1", "target_handle": map[string]any{"kind": "capsule", "canonical_id": "app", "generation": "g1"},
		"epoch": 1, "display": ":100", "xid": 0, "width": w, "height": h,
		"frames_root": StreamFramesRoot, "frame_run_root": StreamRunParent + "/run-1",
		"image_sha256": strings.Repeat("b", 64), "reader_sha256": strings.Repeat("c", 64), "exec_allowlist_sha256": strings.Repeat("d", 64),
		"max_record_bytes": RecordCap(w, h), "max_inflight_frames": 2, "tick_mode": TickModeUnavailable,
	}
}

func testBinding(t *testing.T, w, h int, mutate func(map[string]any)) SeatBinding {
	t.Helper()
	m := bindingMap(w, h)
	if mutate != nil {
		mutate(m)
	}
	raw, err := CanonicalJSON(m)
	if err != nil {
		t.Fatal(err)
	}
	b, err := ParseSeatBinding(raw)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

type frameSpec struct {
	hdr  func(*FrameHeader)
	a, b []byte
}

// streamOf encodes HELLO confirmation, frames and END exactly as the sender does.
func streamOf(bd SeatBinding, frames []frameSpec, endSeq uint64) []byte {
	var out bytes.Buffer
	out.Write(EncodeControl(MagicHelloConfirm, KindHello, bd.SHA256, nil))
	for i, f := range frames {
		hA, _ := Validate(f.a, 0, 0)
		sha, _ := FrameSHA256(f.a, hA)
		payload := uint64(16 + len(f.a) + len(f.b))
		h := FrameHeader{RecordBytes: 128 + payload, Seq: uint64(i + 1), CaptureMonoNS: uint64(1000 * (i + 1)), Width: uint32(bd.Width), Height: uint32(bd.Height), Stride: uint32(3 * bd.Width), PixelFormat: PixelFormatRGB24, Flags: FlagPairUnderGrab, PayloadBytes: payload, SHA256RGB24: sha, BindingSHA256: bd.SHA256}
		if f.hdr != nil {
			f.hdr(&h)
		}
		e := h.Encode()
		out.Write(e[:])
		for _, leg := range [][]byte{f.a, f.b} {
			out.Write(binary.BigEndian.AppendUint64(nil, uint64(len(leg))))
			out.Write(leg)
		}
	}
	out.Write(EncodeControl(MagicEnd, KindEnd, bd.SHA256, EndBody{LastSeq: endSeq, Sent: endSeq}.Encode()))
	return out.Bytes()
}

func goodPair(w, h int, salt byte) frameSpec {
	rgb := synthRGB(w, h, salt)
	return frameSpec{a: synthLeg(w, h, 0, "Xvfb fb", rgb), b: synthLeg(w, h, 1, "srf-fbread xid=7", rgb)}
}

func decodeAll(d *StreamDecoder) ([]StreamRecord, error) {
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

func TestSeatStreamDecodeFragmentedAndCoalesced(t *testing.T) {
	const w, h = 5, 3
	bd := testBinding(t, w, h, nil)
	frames := []frameSpec{goodPair(w, h, 1), goodPair(w, h, 2), goodPair(w, h, 3)}
	if bytes.Equal(frames[0].a[:110], frames[0].b[:110]) {
		t.Fatal("legs must have distinct headers")
	}
	wire := streamOf(bd, frames, 3)
	for name, r := range map[string]io.Reader{"onebyte": iotest.OneByteReader(bytes.NewReader(wire)), "half": iotest.HalfReader(bytes.NewReader(wire)), "coalesced": bytes.NewReader(wire)} {
		recs, err := decodeAll(NewStreamDecoder(r, bd, nil))
		if err != nil || len(recs) != 5 {
			t.Fatalf("%s: %v %d", name, err, len(recs))
		}
		if recs[0].Magic != MagicHelloConfirm || recs[4].Magic != MagicEnd || recs[4].End.ErrorCode != StreamOK || recs[4].End.LastSeq != 3 {
			t.Fatalf("%s: record order", name)
		}
		for i := 1; i <= 3; i++ {
			f := recs[i].Frame
			want := synthRGB(w, h, byte(i))
			if f.Header.Seq != uint64(i) || !bytes.Equal(f.RGB24, want) || f.Header.SHA256RGB24 != sha256.Sum256(want) {
				t.Fatalf("%s: frame %d", name, i)
			}
		}
	}
}

func TestSeatStreamMustFail(t *testing.T) {
	const w, h = 4, 2
	bd := testBinding(t, w, h, nil)
	g := goodPair(w, h, 9)
	torn := goodPair(w, h, 9)
	torn.b = synthLeg(w, h, 1, "srf-fbread xid=7", synthRGB(w, h, 10))
	wrongGeom := frameSpec{a: synthLeg(w+1, h, 0, "fb", synthRGB(w+1, h, 1)), b: synthLeg(w+1, h, 1, "gi", synthRGB(w+1, h, 1))}
	set := func(f func(*FrameHeader)) frameSpec { s := g; s.hdr = f; return s }
	cases := []struct {
		name   string
		frames []frameSpec
		end    uint64
		edit   func([]byte) []byte
		want   StreamCode
	}{
		{"torn legs", []frameSpec{torn}, 1, nil, StreamTorn},
		{"forged sha", []frameSpec{set(func(x *FrameHeader) { x.SHA256RGB24[0] ^= 1 })}, 1, nil, StreamHash},
		{"duplicate seq", []frameSpec{g, set(func(x *FrameHeader) { x.Seq = 1 })}, 2, nil, StreamSeq},
		{"skipped seq", []frameSpec{g, set(func(x *FrameHeader) { x.Seq = 3 })}, 2, nil, StreamSeq},
		{"seq from 0", []frameSpec{set(func(x *FrameHeader) { x.Seq = 0 })}, 1, nil, StreamSeq},
		{"unknown pixel_format", []frameSpec{set(func(x *FrameHeader) { x.PixelFormat = 2 })}, 1, nil, StreamWire},
		{"unknown flag bit", []frameSpec{set(func(x *FrameHeader) { x.Flags |= 1 << 5 })}, 1, nil, StreamWire},
		{"grab flag unset", []frameSpec{set(func(x *FrameHeader) { x.Flags = 0 })}, 1, nil, StreamWire},
		{"tick_valid without committed", []frameSpec{set(func(x *FrameHeader) { x.Flags |= FlagTickValid; x.Tick = 5 })}, 1, nil, StreamTick},
		{"nonzero invalid tick", []frameSpec{set(func(x *FrameHeader) { x.Tick = 5 })}, 1, nil, StreamTick},
		{"wrong binding sha", []frameSpec{set(func(x *FrameHeader) { x.BindingSHA256[31] ^= 1 })}, 1, nil, StreamBinding},
		{"header geometry", []frameSpec{set(func(x *FrameHeader) { x.Width = w + 1; x.Stride = 3 * (w + 1) })}, 1, nil, StreamGeometry},
		{"stride", []frameSpec{set(func(x *FrameHeader) { x.Stride = 4 * w })}, 1, nil, StreamGeometry},
		{"leg geometry", []frameSpec{wrongGeom}, 1, nil, StreamGeometry},
		{"payload_bytes mismatch", []frameSpec{set(func(x *FrameHeader) { x.PayloadBytes-- })}, 1, nil, StreamLength},
		{"record above cap", []frameSpec{set(func(x *FrameHeader) { x.RecordBytes = RecordCap(w, h) + 1; x.PayloadBytes = x.RecordBytes - 128 })}, 1, nil, StreamLength},
		{"header_bytes", []frameSpec{g}, 1, func(b []byte) []byte { b[44+7] = 64; return b }, StreamWire},
		{"version", []frameSpec{g}, 1, func(b []byte) []byte { b[44+5] = 2; return b }, StreamWire},
		{"EOF in header", []frameSpec{g}, 1, func(b []byte) []byte { return b[:44+60] }, StreamWire},
		{"EOF in body", []frameSpec{g}, 1, func(b []byte) []byte { return b[:44+128+40] }, StreamLength},
		{"EOF before END", []frameSpec{g}, 1, func(b []byte) []byte { return b[:len(b)-64] }, StreamWire},
		{"bytes after END", []frameSpec{g}, 1, func(b []byte) []byte { return append(b, 0) }, StreamLength},
		{"END counters", []frameSpec{g}, 2, nil, StreamSeq},
		{"FRAME before HELLO", []frameSpec{g}, 1, func(b []byte) []byte { return b[44:] }, StreamWire},
		{"bad magic", []frameSpec{g}, 1, func(b []byte) []byte { b[44+3] = 'X'; return b }, StreamWire},
	}
	for _, tc := range cases {
		wire := streamOf(bd, tc.frames, tc.end)
		if tc.edit != nil {
			wire = tc.edit(wire)
		}
		d := NewStreamDecoder(iotest.HalfReader(bytes.NewReader(wire)), bd, nil)
		_, err := decodeAll(d)
		if got := StreamCodeOf(err); got != tc.want {
			t.Fatalf("%s: got %v want %s", tc.name, err, StreamCodeName(tc.want))
		}
		if _, again := d.Next(); again != err {
			t.Fatalf("%s: decoder resynchronised after error", tc.name)
		}
	}
}

type countingReader struct {
	r io.Reader
	n int
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += n
	return n, err
}

func TestSeatStreamLengthCapBeforeAllocation(t *testing.T) {
	const w, h = 4, 2
	bd := testBinding(t, w, h, nil)
	huge := uint64(1) << 62
	for _, hdr := range []func(*FrameHeader){
		func(x *FrameHeader) { x.RecordBytes = huge; x.PayloadBytes = huge - 128 },
	} {
		wire := streamOf(bd, []frameSpec{{hdr: hdr, a: goodPair(w, h, 1).a, b: goodPair(w, h, 1).b}}, 1)
		cr := &countingReader{r: bytes.NewReader(wire)}
		var before, after runtime.MemStats
		runtime.GC()
		runtime.ReadMemStats(&before)
		_, err := decodeAll(NewStreamDecoder(cr, bd, nil))
		runtime.ReadMemStats(&after)
		if StreamCodeOf(err) != StreamLength {
			t.Fatal("huge record admitted", err)
		}
		if cr.n != 44+128 {
			t.Fatalf("payload read before cap check: %d bytes", cr.n)
		}
		if delta := after.TotalAlloc - before.TotalAlloc; delta > 1<<20 {
			t.Fatalf("allocated %d bytes for a refused length", delta)
		}
	}
	// Inner leg length above the room/leg cap is refused before allocation too.
	g := goodPair(w, h, 1)
	wire := streamOf(bd, []frameSpec{g}, 1)
	binary.BigEndian.PutUint64(wire[44+128:], math.MaxUint64)
	if _, err := decodeAll(NewStreamDecoder(bytes.NewReader(wire), bd, nil)); StreamCodeOf(err) != StreamLength {
		t.Fatal("huge inner length admitted", err)
	}
	if got := RecordCap(w, h); got != 2*(4*w*h+1<<20)+16+128 {
		t.Fatal("record cap formula", got)
	}
}

func TestCreditLedgerRules(t *testing.T) {
	l := NewCreditLedger(2)
	if l.CanSpend() {
		t.Fatal("HELLO granted credit")
	}
	if StreamCodeOf(l.ApplyAck(1, 2)) != StreamCredit {
		t.Fatal("first ACK must ack seq 0")
	}
	if StreamCodeOf(l.ApplyAck(0, 3)) != StreamCredit {
		t.Fatal("initial inflation admitted")
	}
	if err := l.ApplyAck(0, 2); err != nil || l.Total() != 2 {
		t.Fatal("initial grant", err)
	}
	if err := l.ApplyAck(0, 2); err != nil || l.Total() != 2 {
		t.Fatal("duplicate ACK not idempotent", err)
	}
	for seq := uint64(1); seq <= 2; seq++ {
		if err := l.Spend(); err != nil {
			t.Fatal(err)
		}
		if err := l.MarkSent(seq); err != nil {
			t.Fatal(err)
		}
	}
	if l.CanSpend() || StreamCodeOf(l.Spend()) != StreamCredit {
		t.Fatal("credit exhaustion not enforced")
	}
	for _, bad := range [][2]uint64{{3, 5}, {2, 5}, {0, 1}, {2, 1}, {math.MaxUint64, math.MaxUint64}, {1, math.MaxUint64}} {
		if StreamCodeOf(l.ApplyAck(bad[0], bad[1])) != StreamCredit {
			t.Fatalf("ACK %v admitted", bad)
		}
	}
	if err := l.ApplyAck(2, 4); err != nil || !l.CanSpend() {
		t.Fatal("cumulative grant", err)
	}
	if StreamCodeOf(l.ApplyAck(1, 4)) != StreamCredit {
		t.Fatal("ack regression admitted")
	}
	if l.AckSeq() != 2 || l.Total() != 4 {
		t.Fatal("failed ACK changed state")
	}
}

func TestHostControlEncodingAndCreditedDecode(t *testing.T) {
	const w, h = 3, 2
	bd := testBinding(t, w, h, nil)
	hc := NewHostControl(bd)
	stop := hc.Stop()
	want := append([]byte("W8UC\x00\x01\x00\x03\x00\x00\x00\x00"), bd.SHA256[:]...)
	if !bytes.Equal(stop, want) {
		t.Fatalf("STOP bytes %x", stop)
	}
	hello := hc.Hello()
	if string(hello[:8]) != "W8UC\x00\x01\x00\x01" || binary.BigEndian.Uint32(hello[8:]) != uint32(len(bd.Canonical)) || !bytes.Equal(hello[44:], bd.Canonical) {
		t.Fatal("HELLO layout")
	}
	if sha256.Sum256(hello[44:]) != [32]byte(hello[12:44]) {
		t.Fatal("HELLO digest")
	}
	rec, err := ReadControl(bytes.NewReader(hello), MagicControl)
	if err != nil || rec.Kind != KindHello {
		t.Fatal("HELLO round trip", err)
	}
	if _, err := hc.Ack(0, 3); StreamCodeOf(err) != StreamCredit {
		t.Fatal("host encoded inflated ACK")
	}
	ack, err := hc.Ack(0, 1)
	if err != nil || !bytes.Equal(ack[44:], EncodeAckBody(0, 1)) || binary.BigEndian.Uint16(ack[6:]) != KindAck {
		t.Fatal("ACK layout", err)
	}
	// Two frames on the wire but only one credit granted: E_CREDIT.
	wire := streamOf(bd, []frameSpec{goodPair(w, h, 1), goodPair(w, h, 2)}, 2)
	_, err = decodeAll(NewStreamDecoder(bytes.NewReader(wire), bd, hc.Ledger()))
	if StreamCodeOf(err) != StreamCredit || hc.Ledger().Sent() != 1 {
		t.Fatal("frame without credit admitted", err)
	}
	if _, err := hc.Ack(2, 3); StreamCodeOf(err) != StreamCredit {
		t.Fatal("host acked an unreceived frame")
	}
	// Control parsing refuses unknown kinds, oversize HELLO and bad ACK size.
	for _, raw := range [][]byte{
		EncodeControl(MagicControl, 9, bd.SHA256, nil),
		EncodeControl(MagicControl, KindAck, bd.SHA256, []byte{1}),
		EncodeControl(MagicControl, KindStop, bd.SHA256, []byte{1}),
	} {
		if _, err := ReadControl(bytes.NewReader(raw), MagicControl); StreamCodeOf(err) != StreamWire {
			t.Fatal("control kind/size admitted", err)
		}
	}
	big := EncodeControl(MagicControl, KindHello, bd.SHA256, nil)
	binary.BigEndian.PutUint32(big[8:], MaxHelloBodyBytes+1)
	if _, err := ReadControl(bytes.NewReader(big), MagicControl); StreamCodeOf(err) != StreamLength {
		t.Fatal("oversize HELLO admitted", err)
	}
	if _, err := ReadControl(bytes.NewReader(nil), MagicControl); err != io.EOF {
		t.Fatal("clean EOF", err)
	}
	if _, err := ReadControl(bytes.NewReader(hello[:20]), MagicControl); StreamCodeOf(err) != StreamWire {
		t.Fatal("truncated header", err)
	}
}

func TestSeatBindingCanonicalClosed(t *testing.T) {
	good, err := CanonicalJSON(bindingMap(4, 2))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.HasSuffix(good, []byte("\n")) || !bytes.HasPrefix(good, []byte(`{"container_id":"c1","display":":100","epoch":1,`)) {
		t.Fatalf("canonical form %s", good)
	}
	if _, err := ParseSeatBinding(good); err != nil {
		t.Fatal(err)
	}
	mut := func(f func(map[string]any)) []byte {
		m := bindingMap(4, 2)
		f(m)
		b, _ := CanonicalJSON(m)
		return b
	}
	cases := map[string]struct {
		raw  []byte
		want StreamCode
	}{
		"whitespace":       {append([]byte(" "), good...), StreamBinding},
		"trailing LF":      {append(bytes.Clone(good), '\n'), StreamBinding},
		"unsorted":         {bytes.Replace(good, []byte(`"container_id":"c1","display":":100"`), []byte(`"display":":100","container_id":"c1"`), 1), StreamBinding},
		"float":            {bytes.Replace(good, []byte(`"epoch":1`), []byte(`"epoch":1.0`), 1), StreamBinding},
		"extra key":        {mut(func(m map[string]any) { m["zz"] = "x" }), StreamBinding},
		"missing key":      {mut(func(m map[string]any) { delete(m, "lease_id") }), StreamBinding},
		"handle extra":     {mut(func(m map[string]any) { m["target_handle"].(map[string]any)["x"] = "y" }), StreamBinding},
		"tick_mode":        {mut(func(m map[string]any) { m["tick_mode"] = "maybe" }), StreamBinding},
		"inflight":         {mut(func(m map[string]any) { m["max_inflight_frames"] = 3 }), StreamBinding},
		"zero width":       {mut(func(m map[string]any) { m["width"] = 0 }), StreamGeometry},
		"legacy root":      {mut(func(m map[string]any) { m["frames_root"] = "/dev/shm/w8/frames" }), StreamRoot},
		"run root":         {mut(func(m map[string]any) { m["frame_run_root"] = StreamRunParent + "/other" }), StreamRoot},
		"dotdot run":       {mut(func(m map[string]any) { m["run_id"] = ".."; m["frame_run_root"] = StreamRunParent + "/.." }), StreamRoot},
		"oversize":         {append(bytes.Clone(good), make([]byte, MaxHelloBodyBytes)...), StreamLength},
		"non-ascii string": {bytes.Replace(good, []byte(`"c1"`), []byte("\"c\xc3\xa9\""), 1), StreamBinding},
	}
	for name, tc := range cases {
		if _, err := ParseSeatBinding(tc.raw); StreamCodeOf(err) != tc.want {
			t.Fatalf("%s: got %v want %s", name, err, StreamCodeName(tc.want))
		}
	}
	if b := testBinding(t, 4, 2, func(m map[string]any) { m["max_record_bytes"] = 100 }); b.RecordLimit() != 100 {
		t.Fatal("negotiated cap not enforced")
	}
}

func TestStreamErrorTable(t *testing.T) {
	want := []string{"OK", "E_TORN", "E_WIRE", "E_LENGTH", "E_HASH", "E_SEQ", "E_TICK", "E_BINDING", "E_GEOMETRY", "E_CREDIT", "E_BACKPRESSURE", "E_TIMEOUT", "E_ROOT", "E_RAM_QUOTA"}
	if len(StreamErrorTable) != len(want) {
		t.Fatal("table size")
	}
	for i, e := range StreamErrorTable {
		if e.Code != StreamCode(i) || e.Name != want[i] || StreamCodeName(e.Code) != want[i] || e.Meaning == "" {
			t.Fatalf("row %d %+v", i, e)
		}
	}
	if StreamCodeName(99) != "" || StreamCodeOf(nil) != StreamOK || StreamCodeOf(errors.New("x")) != StreamWire {
		t.Fatal("code mapping")
	}
	if StreamCodeOf(&StreamError{Code: StreamRAMQuota}) != StreamRAMQuota || (&StreamError{Code: StreamHash, Detail: "d"}).Error() != "E_HASH: d" {
		t.Fatal("error text")
	}
}
