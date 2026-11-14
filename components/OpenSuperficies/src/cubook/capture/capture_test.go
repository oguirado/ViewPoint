package capture

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"opensuperficies.local/srf/src/quarantineoracle"
)

func TestXWDHeaderParse(t *testing.T) {
	buf := SynthXWD(8, 4, 2, nil)
	h, err := ParseXWDHeader(bytes.NewReader(buf))
	if err != nil {
		t.Fatal(err)
	}
	if h.Width != 8 || h.Height != 4 || h.BitsPerPixel != 32 || h.BytesPerLine != 32 || h.NColors != 2 {
		t.Fatalf("bad header %+v", h)
	}
	if h.PixelOffset != h.HeaderSize+2*12 {
		t.Fatalf("pixel offset %d want %d", h.PixelOffset, h.HeaderSize+24)
	}
	if _, err := ParseXWDHeader(bytes.NewReader(buf[:40])); err == nil {
		t.Fatal("truncated header accepted")
	}
	bad := SynthXWD(8, 4, 0, nil)
	bad[4*11+3] = 16 // bits_per_pixel low byte (field 11)
	if _, err := ParseXWDHeader(bytes.NewReader(bad)); err == nil {
		t.Fatal("bpp 16 accepted")
	}
}

func TestBGRXToRGB24(t *testing.T) {
	src := []byte{1, 2, 3, 0xff, 10, 20, 30, 0xff} // B,G,R,X x2
	dst := make([]byte, 6)
	BGRXToRGB24(dst, src, 2, 1, 8)
	want := []byte{3, 2, 1, 30, 20, 10}
	if !bytes.Equal(dst, want) {
		t.Fatalf("got %v want %v", dst, want)
	}
	// row padding is skipped
	src = []byte{1, 2, 3, 0, 9, 9, 9, 9, 4, 5, 6, 0, 9, 9, 9, 9}
	dst = make([]byte, 6)
	BGRXToRGB24(dst, src, 1, 2, 8)
	if !bytes.Equal(dst, []byte{3, 2, 1, 6, 5, 4}) {
		t.Fatalf("padding got %v", dst)
	}
}

type fakeFreezer struct {
	log    *[]string
	freeze error
}

var logMu sync.Mutex

func appendLog(l *[]string, e string) { logMu.Lock(); *l = append(*l, e); logMu.Unlock() }

func (f *fakeFreezer) Freeze() error { appendLog(f.log, "freeze"); return f.freeze }
func (f *fakeFreezer) Thaw() error   { appendLog(f.log, "thaw"); return nil }

type logReader struct {
	log *[]string
	r   *bytes.Reader
	d   time.Duration
}

func (l *logReader) ReadAt(p []byte, off int64) (int, error) {
	appendLog(l.log, "read")
	time.Sleep(l.d)
	return l.r.ReadAt(p, off)
}

func newSnapper(t *testing.T, xwd []byte, ring *Ring) (*Snapper, *[]string) {
	t.Helper()
	var log []string
	s := &Snapper{
		Src:      &logReader{log: &log, r: bytes.NewReader(xwd)},
		Freezer:  &fakeFreezer{log: &log},
		Ring:     ring,
		Watchdog: 50 * time.Millisecond,
	}
	return s, &log
}

func TestSnapDeterminismSameStaticScreen(t *testing.T) {
	x := SynthXWD(16, 8, 0, func(px, py int) (r, g, b byte) { return byte(px * 9), byte(py * 17), 7 })
	var sums []string
	for run := 0; run < 2; run++ {
		dir := t.TempDir() // different path each run
		ring := NewRing(dir, 16, 8)
		s, _ := newSnapper(t, x, ring)
		for i := 0; i < 3; i++ {
			if _, err := s.Snap(); err != nil {
				t.Fatal(err)
			}
		}
		sums = append(sums, ring.DetSHA256())
		ring.Close()
	}
	if sums[0] != sums[1] || len(sums[0]) != 64 {
		t.Fatalf("det differ %v", sums)
	}
}

func TestSnapDetectsChange(t *testing.T) {
	a := SynthXWD(16, 8, 0, func(px, py int) (byte, byte, byte) { return 1, 2, 3 })
	b := SynthXWD(16, 8, 0, func(px, py int) (byte, byte, byte) {
		if px == 5 && py == 3 {
			return 1, 2, 4
		}
		return 1, 2, 3
	})
	ring := NewRing(t.TempDir(), 16, 8)
	defer ring.Close()
	sa, _ := newSnapper(t, a, ring)
	sb, _ := newSnapper(t, b, ring)
	fa, err := sa.Snap()
	if err != nil {
		t.Fatal(err)
	}
	fb, err := sb.Snap()
	if err != nil {
		t.Fatal(err)
	}
	if fa.SHA256 == fb.SHA256 {
		t.Fatal("one-pixel change not detected")
	}
	if fb.RenderIndex != fa.RenderIndex+1 {
		t.Fatalf("render_index not monotonic %d %d", fa.RenderIndex, fb.RenderIndex)
	}
}

func TestSnapFreezeThawOrderAndWatchdog(t *testing.T) {
	x := SynthXWD(8, 4, 0, nil)
	ring := NewRing(t.TempDir(), 8, 4)
	defer ring.Close()
	s, log := newSnapper(t, x, ring)
	if _, err := s.Snap(); err != nil {
		t.Fatal(err)
	}
	if strings.ReplaceAll(strings.Join(*log, ","), "read,read", "read") != "freeze,read,thaw" {
		t.Fatalf("order %v", *log)
	}
	// slow read beyond the 50 ms watchdog: thaw still happens, error is a gate stop
	var log2 []string
	s2 := &Snapper{
		Src:      &logReader{log: &log2, r: bytes.NewReader(x), d: 120 * time.Millisecond},
		Freezer:  &fakeFreezer{log: &log2},
		Ring:     ring,
		Watchdog: 50 * time.Millisecond,
	}
	_, err := s2.Snap()
	var ce *Error
	if !errors.As(err, &ce) || ce.Reason != "freeze_watchdog" {
		t.Fatalf("want freeze_watchdog got %v", err)
	}
	thaws := 0
	for _, e := range log2 {
		if e == "thaw" {
			thaws++
		}
	}
	if thaws != 1 {
		t.Fatalf("thaw count %d log %v", thaws, log2)
	}
	// freeze failure: no read, no frame
	var log3 []string
	s3 := &Snapper{Src: &logReader{log: &log3, r: bytes.NewReader(x)}, Freezer: &fakeFreezer{log: &log3, freeze: errors.New("x")}, Ring: ring, Watchdog: 50 * time.Millisecond}
	if _, err := s3.Snap(); err == nil {
		t.Fatal("freeze error swallowed")
	}
	for _, e := range log3 {
		if e == "read" {
			t.Fatal("read after failed freeze")
		}
	}
}

func snapN(t *testing.T, ring *Ring, n int) {
	x := SynthXWD(16, 8, 0, func(px, py int) (byte, byte, byte) { return byte(px), 0, 0 })
	s, _ := newSnapper(t, x, ring)
	for i := 0; i < n; i++ {
		if _, err := s.Snap(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestManifestFrameV1Closed(t *testing.T) {
	dir := t.TempDir()
	ring := NewRing(dir, 16, 8)
	snapN(t, ring, 6) // ring keeps the last 4
	m := ring.Manifest("cockpit", "idle")
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	if err := ValidateManifestV1(raw); err != nil {
		t.Fatalf("valid manifest rejected: %v", err)
	}
	var o map[string]any
	json.Unmarshal(raw, &o)
	if o["schema"] != "vqa.frame/v1" || o["storage"] != "rgb24_raw" || o["raw_path"] != "frames.raw" ||
		o["channels"] != float64(3) || o["pixel_format"] != "rgb24" || o["count"] != float64(4) {
		t.Fatalf("manifest fields %v", o)
	}
	fr := o["frames"].([]any)
	f0 := fr[0].(map[string]any)
	for _, k := range []string{"i", "t_us", "render_index", "sha256", "file_sha256"} {
		if _, ok := f0[k]; !ok {
			t.Fatalf("frame field %s missing", k)
		}
	}
	if f0["render_index"] != float64(2) || f0["i"] != float64(0) {
		t.Fatalf("ring should keep render_index 2..5, got %v", f0)
	}
	// sha is over the RGB24 bytes
	b := ring.Bytes(0)
	sum := sha256.Sum256(b)
	if f0["sha256"] != hex.EncodeToString(sum[:]) {
		t.Fatal("sha256 not over rgb24 bytes")
	}
	// closed: unknown field, float, bad enum
	for _, mut := range []func(map[string]any){
		func(o map[string]any) { o["extra"] = 1 },
		func(o map[string]any) { o["width"] = 16.5 },
		func(o map[string]any) { o["storage"] = "bmp" },
		func(o map[string]any) { o["frames"].([]any)[0].(map[string]any)["bogus"] = 1 },
		func(o map[string]any) { o["count"] = float64(3) },
	} {
		var c map[string]any
		json.Unmarshal(raw, &c)
		mut(c)
		bad, _ := json.Marshal(c)
		if ValidateManifestV1(bad) == nil {
			t.Fatalf("mutated manifest accepted: %s", bad)
		}
	}
	// file really holds 4 slots
	st, err := os.Stat(filepath.Join(dir, "frames.raw"))
	if err != nil || st.Size() != int64(4*16*8*3) {
		t.Fatalf("frames.raw size %v err %v", st, err)
	}
}

func TestReceiptPathIndependent(t *testing.T) {
	var recs [][]byte
	for run := 0; run < 2; run++ {
		dir := t.TempDir()
		ring := NewRing(dir, 16, 8)
		snapN(t, ring, 4)
		r := ring.Receipt(Header{HeaderSize: 100, NColors: 0, BitsPerPixel: 32, BytesPerLine: 64, PixelOffset: 100}, "tmpfs", 1234)
		b, _ := json.Marshal(r)
		if strings.Contains(string(b), dir) || strings.Contains(string(b), "/dev/shm") {
			t.Fatalf("receipt leaks path: %s", b)
		}
		var o map[string]any
		json.Unmarshal(b, &o)
		for _, k := range []string{"schema", "det_sha256", "frames", "geometry", "xwd_header", "ram_peak_bytes", "ramws_fstype"} {
			if _, ok := o[k]; !ok {
				t.Fatalf("receipt field %s missing", k)
			}
		}
		if o["schema"] != "os.cu.capture-receipt/v1" {
			t.Fatal("schema")
		}
		// det_sha256 only: strip non-deterministic bits and compare
		recs = append(recs, []byte(r.DetSHA256))
		ring.Close()
	}
	if !bytes.Equal(recs[0], recs[1]) {
		t.Fatal("det_sha256 depends on run/path")
	}
}

func fsOf(s string) func(string) string { return func(string) string { return s } }

func TestRefuseNonRamPath(t *testing.T) {
	root := t.TempDir()
	g := PathGuard{Root: root, FSType: fsOf("tmpfs")}
	if err := g.Check(filepath.Join(root, "run1", "frames.raw")); err != nil {
		t.Fatalf("path under root refused: %v", err)
	}
	for _, p := range []string{"/tmp/x/frames.raw", filepath.Join(root, "..", "evil"), "relative/frames.raw", root + "x/frames.raw"} {
		err := g.Check(p)
		var ce *Error
		if !errors.As(err, &ce) || ce.Reason != "E_RAM_NOT_TMPFS" {
			t.Fatalf("%q: want E_RAM_NOT_TMPFS got %v", p, err)
		}
	}
	// under root but on a disk filesystem
	g2 := PathGuard{Root: root, FSType: fsOf("ext4")}
	if err := g2.Check(filepath.Join(root, "run1")); err == nil {
		t.Fatal("ext4 accepted")
	}
	// symlink escape from inside root
	os.Symlink("/tmp", filepath.Join(root, "esc"))
	if err := g.Check(filepath.Join(root, "esc", "frames.raw")); err == nil {
		t.Fatal("symlink escape accepted")
	}
	// the default root is the STORAGE LAW root
	if DefaultRamRoot != "/dev/shm/vqa-ramws/opensuperficies/os-cu-0" {
		t.Fatal(DefaultRamRoot)
	}
}

func TestHostDisplayRefused(t *testing.T) {
	for _, d := range []string{":0", ":1", ":10", "", ":101", "host:100"} {
		err := GuardDisplay(d, true)
		var ce *Error
		if !errors.As(err, &ce) || ce.Reason != "host_display" {
			t.Fatalf("%q: %v", d, err)
		}
	}
	if err := GuardDisplay(":100", false); err == nil {
		t.Fatal(":100 outside the capsule netns accepted")
	}
	if err := GuardDisplay(":100", true); err != nil {
		t.Fatal(err)
	}
	iso := strings.Repeat("ab", 16)
	if err := GuardIso(iso, iso); err != nil {
		t.Fatal(err)
	}
	err := GuardIso(iso, strings.Repeat("cd", 16))
	var ce *Error
	if !errors.As(err, &ce) || ce.Reason != "iso_mismatch" {
		t.Fatalf("iso: %v", err)
	}
}

func TestRunArgsVectorSha(t *testing.T) {
	cfg := RunConfig{Slot: 3, Image: "opensuperficies-desktop-arm64:1.0.0", Iso: strings.Repeat("ab", 16), FBDir: "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/r1/fb", Entrypoint: "/repo/docker/cu/entrypoint-cu.sh"}
	a := RunArgs(cfg)
	joined := strings.Join(a, " ")
	for _, want := range []string{"--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--pids-limit 256",
		"--cpuset-cpus 0,1", "--memory 4g", "--tmpfs /run/newton-tenant:rw,nosuid,nodev,mode=0700", "--tmpfs /tmp:rw,nosuid,nodev",
		"-v /dev/shm/vqa-ramws/opensuperficies/os-cu-0/r1/fb:/run/vqa/fb:rw", "-v /repo/docker/cu/entrypoint-cu.sh:/usr/local/bin/entrypoint.sh:ro",
		"-e XPRA_MODE=off", "-e VQA_ISO=" + cfg.Iso} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in %s", want, joined)
		}
	}
	for _, bad := range []string{"--privileged", "docker.sock", "/tmp/.X11-unix", "/dev/dri", "/dev/uinput", "--network host", "-p "} {
		if strings.Contains(joined, bad) {
			t.Fatalf("forbidden %q", bad)
		}
	}
	if a[len(a)-1] != cfg.Image {
		t.Fatal("image must be last")
	}
	s1, s2 := RunArgsSHA256(a), RunArgsSHA256(RunArgs(cfg))
	if s1 != s2 || len(s1) != 64 {
		t.Fatal("sha unstable")
	}
	cfg.Slot = 4
	if RunArgsSHA256(RunArgs(cfg)) == s1 {
		t.Fatal("slot not in vector")
	}
	cfg.Slot = 9
	if err := cfg.Validate(); err == nil {
		t.Fatal("slot 9 accepted")
	}
}

func TestEntrypointCuFlags(t *testing.T) {
	b, err := os.ReadFile("../../../docker/cu/entrypoint-cu.sh")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	for _, want := range []string{"-fbdir /run/vqa/fb", "+extension XTEST", "-nolisten tcp", "-nolisten unix", "-nocursor",
		`XPRA_MODE="${XPRA_MODE:-off}"`, "_OPENNEWTON_ISOLATION_ID", "$VQA_ISO", `GEOMETRY="${GEOMETRY:-1280x1064x24}"`, "LP_NUM_THREADS=2", `DISPLAY_NUM="${DISPLAY_NUM:-100}"`} {
		if !strings.Contains(s, want) {
			t.Fatalf("entrypoint-cu.sh lacks %q", want)
		}
	}
	if strings.Contains(s, "LP_NUM_THREADS=4") {
		t.Fatal("LP_NUM_THREADS=4 leaks past cpuset 0,1")
	}
}

func TestCapsuleV1SchemaClosed(t *testing.T) {
	c := Capsule{Slot: 2, ContainerID: strings.Repeat("a", 64), ImageDigest: "sha256:" + strings.Repeat("b", 64),
		RunArgsSHA256: strings.Repeat("c", 64), IsoSHA256: strings.Repeat("d", 64), Geometry: [3]int{1280, 1064, 24},
		Cpuset: "0,1", XWDHeader: XWDHeaderJSON{HeaderSize: 100, NColors: 0, BPP: 32, BPL: 5120, PixelOffset: 100},
		Quarantine: Quarantine{Doors: map[string]int{"A": 1, "B": 0}, RC: 5}}
	raw, err := json.Marshal(c.withSchema())
	if err != nil {
		t.Fatal(err)
	}
	if err := ValidateCapsuleV1(raw); err != nil {
		t.Fatal(err)
	}
	var o map[string]any
	json.Unmarshal(raw, &o)
	if o["schema"] != "vqa.capsule/v1" {
		t.Fatal("schema")
	}
	for _, mut := range []func(map[string]any){
		func(o map[string]any) { o["extra"] = 1 },
		func(o map[string]any) { o["slot"] = float64(8) },
		func(o map[string]any) { o["container_id"] = "short" },
		func(o map[string]any) { o["geometry"] = []any{1.5, 2, 3} },
		func(o map[string]any) { delete(o, "quarantine") },
		func(o map[string]any) { o["quarantine"].(map[string]any)["doors"].(map[string]any)["A"] = 2 },
	} {
		var c2 map[string]any
		json.Unmarshal(raw, &c2)
		mut(c2)
		bad, _ := json.Marshal(c2)
		if ValidateCapsuleV1(bad) == nil {
			t.Fatalf("mutated capsule accepted: %s", bad)
		}
	}
}

func TestQuarantineMapping(t *testing.T) {
	q := QuarantineFromDoors([]Door{{Puerta: "A", Verde: true}, {Puerta: "B", Verde: true}})
	if q.RC != 0 || q.Doors["A"] != 1 || q.Doors["B"] != 1 {
		t.Fatalf("%+v", q)
	}
	q = QuarantineFromDoors([]Door{{Puerta: "A", Verde: true}, {Puerta: "N", Verde: false}})
	if q.RC != 5 || q.Doors["N"] != 0 {
		t.Fatalf("%+v", q)
	}
}

func TestInCapsuleFromProbeNotFlag(t *testing.T) {
	none := CapsuleProbe{Exists: func(string) bool { return false }}
	if InCapsule(none) || InCapsule(CapsuleProbe{}) {
		t.Fatal("host reported as capsule")
	}
	if GuardDisplay(":100", InCapsule(none)) == nil {
		t.Fatal("host :100 accepted")
	}
	dk := CapsuleProbe{Exists: func(p string) bool { return p == "/.dockerenv" }}
	if !InCapsule(dk) || GuardDisplay(":100", InCapsule(dk)) != nil {
		t.Fatal("capsule marker ignored")
	}
}

func TestCheckRunDirManaged(t *testing.T) {
	root := t.TempDir()
	g := PathGuard{Root: root, FSType: fsOf("tmpfs")}
	run := filepath.Join(root, "r1")
	os.Mkdir(run, 0o700)
	if err := g.CheckRunDir(run); err == nil {
		t.Fatal("dir without owner.json/.lock accepted")
	}
	os.WriteFile(filepath.Join(run, "owner.json"), []byte("{}"), 0o600)
	lk, _ := os.OpenFile(filepath.Join(run, ".lock"), os.O_RDWR|os.O_CREATE, 0o600)
	defer lk.Close()
	if err := g.CheckRunDir(run); err == nil {
		t.Fatal("unheld .lock accepted")
	}
	syscall.Flock(int(lk.Fd()), syscall.LOCK_EX)
	if err := g.CheckRunDir(run); err != nil {
		t.Fatal(err)
	}
	deep := filepath.Join(run, "deep")
	os.Mkdir(deep, 0o700)
	if g.CheckRunDir(deep) == nil || g.CheckRunDir(root) == nil {
		t.Fatal("non-direct-child accepted")
	}
}

func TestCheckOutResolvesSymlinks(t *testing.T) {
	ln := filepath.Join(t.TempDir(), "s")
	os.Symlink("/dev/shm", ln)
	if CheckOut(filepath.Join(ln, "x"), nil) == nil {
		t.Fatal("symlink into /dev/shm accepted")
	}
	if CheckOut(filepath.Join(t.TempDir(), "x"), fsOf("tmpfs")) == nil {
		t.Fatal("tmpfs out accepted")
	}
	if err := CheckOut(filepath.Join(t.TempDir(), "x"), fsOf("ext4")); err != nil {
		t.Fatal(err)
	}
}

func TestRingNeverCreatesRunDir(t *testing.T) {
	d := filepath.Join(t.TempDir(), "absent")
	r := NewRing(d, 2, 2)
	if _, err := r.Put(make([]byte, 12), 0); err == nil {
		t.Fatal("ring created/used a missing dir")
	}
	if _, err := os.Stat(d); err == nil {
		t.Fatal("run dir created by the ring")
	}
}

// ADC-09 M8: QuarantineFromOracle must go red on !Verde and carry doors.
func TestQuarantineFromOracle(t *testing.T) {
	green := quarantineoracle.CuarentenaResultado{Verde: true, Puertas: []quarantineoracle.PuertaResultado{{Puerta: "A", Verde: true}, {Puerta: "B", Verde: true}}}
	if q := QuarantineFromOracle(green); q.RC != 0 || q.Doors["A"] != 1 || q.Doors["B"] != 1 {
		t.Fatalf("green %+v", q)
	}
	// overall verdict red while every listed door is green: still rc 5
	red := green
	red.Verde = false
	if q := QuarantineFromOracle(red); q.RC != 5 {
		t.Fatalf("red verdict ignored %+v", q)
	}
	one := quarantineoracle.CuarentenaResultado{Verde: false, Puertas: []quarantineoracle.PuertaResultado{{Puerta: "A", Verde: true}, {Puerta: "N", Verde: false}}}
	if q := QuarantineFromOracle(one); q.RC != 5 || q.Doors["N"] != 0 || q.Doors["A"] != 1 {
		t.Fatalf("door N %+v", q)
	}
}
