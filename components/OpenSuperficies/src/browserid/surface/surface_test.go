package surface

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"opensuperficies.local/srf/src/browserid/inject"
	"opensuperficies.local/srf/src/browserid/store"
	"opensuperficies.local/srf/src/browserid/tiling"
	"opensuperficies.local/srf/src/programsurface"
)

type fakeRunner struct {
	proxy       bool
	ready       string
	list        string
	geom        map[string]string
	calls       [][]string
	focus       string
	names       string
	badGeometry bool
	pair        []byte
	missing     bool
}

func (r *fakeRunner) Run(_ context.Context, a []string, _ []byte) ([]byte, error) {
	r.calls = append(r.calls, slices.Clone(a))
	s := strings.Join(a, " ")
	if r.missing && strings.HasPrefix(s, "docker exec") {
		return nil, exec.ErrNotFound
	}
	switch {
	case strings.Contains(s, downloadsPython):
		return []byte("[]"), nil
	case strings.Contains(s, "network inspect"):
		return []byte(`{"Name":"srf-idnet","Internal":true,"IPAM":{"Config":[{"Subnet":"172.30.80.0/24"}]}}`), nil
	case strings.Contains(s, "inspect --format {{.State.Running}}"):
		if r.proxy {
			return []byte("true\n"), nil
		}
		return []byte("false\n"), nil
	case strings.Contains(s, "inspect"):
		return []byte("172.30.80.10\n"), nil
	case strings.Contains(s, "logs"):
		if r.ready != "" {
			return []byte("xpra layer up\n" + r.ready), nil
		}
		return []byte("xpra layer up\nIDENTITY_READY {\"windows\":[{\"id\":\"w1\",\"x\":0,\"y\":0,\"w\":1280,\"h\":900,\"xid\":\"0x123\"}]}\n"), nil
	case strings.Contains(s, "ps -a"):
		return []byte(r.names), nil
	case strings.Contains(s, "wmctrl -lG"):
		if r.list != "" {
			return []byte(r.list), nil
		}
		return []byte("0x123 0 0 0 1280 900 host Browser\n"), nil
	case strings.Contains(s, "getwindowgeometry"):
		if value, ok := r.geom[a[len(a)-1]]; ok {
			return []byte(value), nil
		}
		x := 0
		if r.badGeometry {
			x = 1
		}
		return []byte(fmt.Sprintf("WINDOW=291\nX=%d\nY=0\nWIDTH=1280\nHEIGHT=900\nSCREEN=0\n", x)), nil
	case strings.Contains(s, "getwindowfocus"):
		return []byte(r.focus), nil
	case strings.Contains(s, "getwindowname"):
		return []byte("Example https://example.org/a\n"), nil
	case strings.Contains(s, "pair :100 0"):
		return r.pair, nil
	}
	return nil, nil
}

type fakeLaunch struct {
	plans   []Launch
	removes int
	fail    bool
}

func (l *fakeLaunch) Available() error { return nil }
func (l *fakeLaunch) Start(_ context.Context, p Launch) error {
	l.plans = append(l.plans, p)
	if l.fail {
		return errors.New("failed")
	}
	return nil
}
func (l *fakeLaunch) Remove(context.Context, string) error { l.removes++; return nil }

type sink struct {
	rgb  []byte
	w, h int
}

func (s *sink) Write(_ string, _ string, seq int, rgb []byte, w, h int) (string, error) {
	s.rgb = slices.Clone(rgb)
	s.w = w
	s.h = h
	return fmt.Sprintf("/dev/shm/test/f%06d.png", seq), nil
}
func setup(t *testing.T) (*Tool, *fakeRunner, *fakeLaunch, *sink) {
	t.Helper()
	root := t.TempDir()
	if err := os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	st, e := store.New(filepath.Join(root, "store"))
	if e != nil {
		t.Fatal(e)
	}
	if e = st.Create(store.Identity{ProfileID: "identity-a", Browser: "chromium", AccountLabel: "test", AllowedDomains: []string{"example.org"}}); e != nil {
		t.Fatal(e)
	}
	r := &fakeRunner{proxy: true, focus: "291\n"}
	l := &fakeLaunch{}
	s := &sink{}
	tool := &Tool{R: r, Clock: func() uint64 { return 42 }, Root: filepath.Join(root, "state"), Store: st, StoreRoot: filepath.Join(root, "store"), Registry: filepath.Join(root, "registry.json"), Proxy: "http://172.30.80.2:3128", Launcher: l, Sink: s, ProfileCheck: func(string) error { return nil }}
	return tool, r, l, s
}
func start(t *testing.T, tool *Tool) Capsule {
	t.Helper()
	c, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1})
	if e != nil {
		t.Fatal(e)
	}
	return c
}
func TestIdentitySurfaceStartValidation(t *testing.T) {
	tool, r, l, _ := setup(t)
	ctx := context.Background()
	if _, e := tool.Start(ctx, StartSpec{Identity: "../x", Windows: 1}); Code(e) != 2 {
		t.Fatal(e)
	}
	if len(r.calls) != 0 || len(l.plans) != 0 {
		t.Fatal("invalid request reached runner")
	}
	c := start(t, tool)
	if c.Container != "srf-id-test" || len(c.Windows) != 1 {
		t.Fatal(c)
	}
	a := l.plans[0].Argv
	joined := strings.Join(a, " ")
	for _, v := range []string{"--network srf-idnet", "--cap-drop ALL", "--memory 4g", "--pids-limit 256", "--read-only", "SRF_EGRESS_PROXY=http://172.30.80.2:3128", "dst=/profile", "--user 10001:10001", "--tmpfs /run/vqa/fb:", "--security-opt seccomp="} {
		if !strings.Contains(joined, v) {
			t.Fatalf("missing %s", v)
		}
	}
	for _, v := range []string{"--no-sandbox", "remote-debugging", "--network host", "/var/run/docker.sock"} {
		if strings.Contains(joined, v) {
			t.Fatal(joined)
		}
	}
	b, _ := os.ReadFile(tool.Registry)
	if !strings.Contains(string(b), `"srf-id-test":"172.30.80.10"`) {
		t.Fatal(string(b))
	}
	if _, e := tool.Start(ctx, StartSpec{Identity: "identity-a", Capsule: "srf-id-other", Windows: 1}); Code(e) != 4 {
		t.Fatal("exclusivity", e)
	}
}
func TestIdentitySurfaceUnavailableLauncher(t *testing.T) {
	tool, r, _, _ := setup(t)
	tool.Launcher = UnavailableLauncher{}
	_, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Windows: 1})
	if Code(e) != 3 || len(r.calls) != 0 {
		t.Fatal(e, r.calls)
	}
	lease, e := tool.Store.Acquire("identity-a", "other")
	if e != nil {
		t.Fatal("refusal leaked lease", e)
	}
	_ = tool.Store.Release(lease)
}
func TestIdentitySurfaceInputFocus(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	ctx := context.Background()
	r.focus = "999\n"
	a, e := tool.Input(ctx, c.Container, "w1", inject.Request{Kind: inject.KindKey, Keysym: "Tab"})
	if Code(e) != 6 || a.FocusVerified {
		t.Fatal(a, e)
	}
	for _, v := range r.calls {
		if strings.Contains(strings.Join(v, " "), "xdotool key") {
			t.Fatal("input after mismatch")
		}
	}
	r.focus = "291\n"
	a, e = tool.Input(ctx, c.Container, "w1", inject.Request{Kind: inject.KindClick, X: 1280, Y: 0, Button: 1})
	if Code(e) != 2 || a.FocusVerified {
		t.Fatal(a, e)
	}
	a, e = tool.Input(ctx, c.Container, "w1", inject.Request{Kind: inject.KindClick, X: 1, Y: 2, Button: 1})
	if e != nil || !a.FocusVerified || a.InputMonoNS != 42 {
		t.Fatal(a, e)
	}
	r.badGeometry = true
	if _, e = tool.Input(ctx, c.Container, "w1", inject.Request{Kind: inject.KindKey, Keysym: "Tab"}); Code(e) != 6 {
		t.Fatal("moved window", e)
	}
}
func TestIdentitySurfaceStopVerify(t *testing.T) {
	tool, r, l, _ := setup(t)
	c := start(t, tool)
	r.names = c.Container
	if e := tool.Stop(context.Background(), c.Container); Code(e) != 6 {
		t.Fatal(e)
	}
	if _, e := tool.Store.Acquire("identity-a", "other"); e == nil {
		t.Fatal("released live identity")
	}
	r.names = ""
	if e := tool.Stop(context.Background(), c.Container); e != nil {
		t.Fatal(e)
	}
	if l.removes != 2 {
		t.Fatal(l.removes)
	}
	if _, e := tool.Store.Acquire("identity-a", "other"); e != nil {
		t.Fatal(e)
	}
}
func TestFrameAndAXReportMissingDockerNotGeometry(t *testing.T) {
	// Measured: a caller with an empty PATH got "window geometry changed before capture".
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	r.missing = true
	if _, e := tool.Frame(context.Background(), c.Container, "w1"); Code(e) != 4 || !strings.Contains(e.Error(), "docker not found on PATH") {
		t.Fatal(e)
	}
	if _, e := tool.AX(context.Background(), c.Container, "w1", ""); Code(e) == 6 && strings.Contains(e.Error(), "geometry") {
		t.Fatal(e)
	}
}
func TestTypedTextNeverAppearsInArgv(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	for _, paste := range []bool{false, true} {
		r.calls = nil
		var e error
		if paste {
			_, e = tool.Paste(context.Background(), c.Container, "w1", inject.Request{Kind: inject.KindText, Text: "hunter2-secret"})
		} else {
			_, e = tool.Input(context.Background(), c.Container, "w1", inject.Request{Kind: inject.KindText, Text: "hunter2-secret"})
		}
		if e != nil {
			t.Fatal(e)
		}
		for _, a := range r.calls {
			if strings.Contains(strings.Join(a, " "), "hunter2") {
				t.Fatal("text leaked into argv", paste, a)
			}
		}
	}
}

func TestPasteEntersTextThroughCapsuleClipboard(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	action, e := tool.Paste(context.Background(), c.Container, "w1", inject.Request{Kind: inject.KindText, Text: "Caf\u00e9 compact."})
	if e != nil || !action.FocusVerified {
		t.Fatal(action, e)
	}
	found := false
	for _, a := range r.calls {
		s := strings.Join(a, " ")
		if strings.HasPrefix(s, "docker exec -i -u 10001:10001 -e DISPLAY=") && strings.Contains(s, "python3 -I -c") && strings.Contains(s, "SELECTION_CLIPBOARD") {
			found = true
		}
		if strings.Contains(s, "xdotool type") {
			t.Fatal("paste must not type")
		}
	}
	if !found {
		t.Fatal("clipboard helper not run")
	}
	if _, e := tool.Paste(context.Background(), c.Container, "w1", inject.Request{Kind: inject.KindKey, Keysym: "Return"}); Code(e) != 2 {
		t.Fatal(e)
	}
}
func TestIdentitySurfaceFrameKeys(t *testing.T) {
	tool, r, _, s := setup(t)
	c := start(t, tool)
	r.pair = pairFixture(1280, 900)
	f, e := tool.Frame(context.Background(), c.Container, "w1")
	if e != nil {
		t.Fatal(e)
	}
	sha := sha256.Sum256(s.rgb)
	if f.SHA256RGB24 != hex.EncodeToString(sha[:]) || f.Width != 1280 || f.Height != 900 || f.Generation != c.Container || f.Seq != 1 {
		t.Fatal(f)
	}
	b, _ := json.Marshal(f)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	keys := []string{"path", "seq", "sha256_rgb24", "width", "height", "capture_mono_ns", "generation", "window"}
	if len(m) != len(keys) {
		t.Fatal(string(b))
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			t.Fatal(k)
		}
	}
	if f.Window.Title != "Example https://example.org/a" || f.Window.URL != "https://example.org/a" {
		t.Fatal(f.Window)
	}
	r.pair[len(r.pair)-2] ^= 1
	if _, e = tool.Frame(context.Background(), c.Container, "w1"); Code(e) != 5 {
		t.Fatal("mismatched pair", e)
	}
}
func TestIdentitySurfaceCrop(t *testing.T) {
	rgb := []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
	out, e := CropRGB(rgb, 2, 2, Window{X: 1, Y: 0, W: 1, H: 2})
	if e != nil || !slices.Equal(out, []byte{3, 4, 5, 9, 10, 11}) {
		t.Fatal(out, e)
	}
	if _, e = CropRGB(rgb, 2, 2, Window{X: 2, Y: 0, W: 1, H: 2}); e == nil {
		t.Fatal("bounds")
	}
}
func TestIdentitySurfaceLoginJQueryUnavailable(t *testing.T) {
	tool, _, _, _ := setup(t)
	c := start(t, tool)
	tool.jqueryPath = filepath.Join(t.TempDir(), "absent-jquery")
	if _, e := tool.Login(context.Background(), c.Container); Code(e) != 4 {
		t.Fatal(e)
	}
}

// XWD header and pixels are synthetic; no X server or image file is used.
func pairFixture(w, h int) []byte {
	const header = 100
	leg := make([]byte, header+4*w*h)
	fields := []uint32{100, 7, 2, 24, uint32(w), uint32(h), 0, 0, 32, 0, 32, 32, uint32(w * 4), 4, 0xff0000, 0xff00, 0xff, 8, 256, 0, uint32(w), uint32(h), 0, 0, 0}
	for i, v := range fields {
		binary.BigEndian.PutUint32(leg[i*4:], v)
	}
	for i := header; i < len(leg); i += 4 {
		leg[i] = 7
		leg[i+1] = 8
		leg[i+2] = 9
	}
	var out []byte
	for range 2 {
		out = binary.BigEndian.AppendUint64(out, uint64(len(leg)))
		out = append(out, leg...)
	}
	return out
}

func TestIdentitySurfacePartialStartRetainsLease(t *testing.T) {
	tool, r, l, _ := setup(t)
	l.fail = true
	c, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1})
	if Code(e) != 6 || c.Container != "srf-id-test" {
		t.Fatal(c, e)
	}
	if _, e = tool.Store.Acquire("identity-a", "other"); e == nil {
		t.Fatal("ambiguous launch lease released")
	}
	r.names = ""
	if e = tool.Stop(context.Background(), c.Container); e != nil {
		t.Fatal(e)
	}
}
func TestIdentitySurfacePlanMultiple(t *testing.T) {
	for _, n := range []int{2, 4, 16} {
		layout, e := tiling.Plan(tiling.Spec{ProfileID: "identity-a", DisplayWidth: Width, DisplayHeight: Height, Windows: n, MinWidth: 320, MinHeight: 225})
		if e != nil || len(layout.Windows) != n {
			t.Fatal(n, layout, e)
		}
	}
}
func TestIdentitySurfacePrivateState(t *testing.T) {
	tool, _, _, _ := setup(t)
	c := start(t, tool)
	if e := os.Chmod(tool.statePath(c.Container), 0644); e != nil {
		t.Fatal(e)
	}
	if _, e := tool.Frame(context.Background(), c.Container, "w1"); Code(e) != 4 {
		t.Fatal("insecure lease file accepted", e)
	}
}
func TestIdentitySurfaceStateNilMapRefused(t *testing.T) {
	tool, _, _, _ := setup(t)
	c := start(t, tool)
	b, e := os.ReadFile(tool.statePath(c.Container))
	if e != nil {
		t.Fatal(e)
	}
	var s session
	if e = json.Unmarshal(b, &s); e != nil {
		t.Fatal(e)
	}
	s.Seq = nil
	if e = tool.save(s); e != nil {
		t.Fatal(e)
	}
	if _, e = tool.Frame(context.Background(), c.Container, "w1"); Code(e) != 6 {
		t.Fatal("nil state map", e)
	}
}
func TestIdentitySurfaceRegistryMerge(t *testing.T) {
	tool, _, _, _ := setup(t)
	if e := tool.registry("srf-id-other", "172.30.80.11", false); e != nil {
		t.Fatal(e)
	}
	_ = start(t, tool)
	b, e := os.ReadFile(tool.Registry)
	if e != nil || !strings.Contains(string(b), "srf-id-other") {
		t.Fatal(string(b), e)
	}
	if e = tool.registry("srf-id-conflict", "172.30.80.11", false); Code(e) != 6 {
		t.Fatal("IP collision", e)
	}
}
func TestIdentitySurfaceRefusesDiskSinkAndTraversal(t *testing.T) {
	if _, e := (ramSink{}).Write("../bad", "w1", 1, []byte{1, 2, 3}, 1, 1); Code(e) != 2 {
		t.Fatal(e)
	}
	if _, e := (ramSink{}).Write("srf-id-test", "../x", 1, []byte{1, 2, 3}, 1, 1); Code(e) != 2 {
		t.Fatal(e)
	}
}

func TestIdentitySurfaceLockCancellation(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	unlock, e := tool.lock(context.Background())
	if e != nil {
		t.Fatal(e)
	}
	defer unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	before := len(r.calls)
	if _, e = tool.Input(ctx, c.Container, "w1", inject.Request{Kind: inject.KindKey, Keysym: "Tab"}); Code(e) != 6 {
		t.Fatal(e)
	}
	if len(r.calls) != before {
		t.Fatal("input bypassed control lock")
	}
}
func TestIdentitySurfaceAlreadyCancelled(t *testing.T) {
	tool, r, l, _ := setup(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, e := tool.Start(ctx, StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1}); Code(e) != 6 {
		t.Fatal(e)
	}
	if len(r.calls) != 0 || len(l.plans) != 0 {
		t.Fatal("cancelled launch reached runner")
	}
}

func TestIdentitySurfaceImageReadiness(t *testing.T) {
	for _, tc := range []struct {
		name, ready string
		code        int
	}{
		{"valid", `IDENTITY_READY {"windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"xid":"0x123"}]}`, 0},
		{"wrongXID", `IDENTITY_READY {"windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"xid":"0x999"}]}`, 6},
		{"wrongTile", `IDENTITY_READY {"windows":[{"id":"w1","x":1,"y":0,"w":1280,"h":900,"xid":"0x123"}]}`, 6},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tool, r, _, _ := setup(t)
			r.ready = tc.ready
			_, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1})
			if Code(e) != tc.code {
				t.Fatal(e)
			}
		})
	}
}
func TestIdentitySurfaceSecondWindowCoordinatesAndCapture(t *testing.T) {
	tool, r, _, s := setup(t)
	r.focus = "292"
	r.list = "0x123 0 0 0 640 900 host Browser\n0x124 0 640 0 640 900 host Browser\n"
	r.ready = `IDENTITY_READY {"windows":[{"id":"w1","x":0,"y":0,"w":640,"h":900,"xid":"0x123"},{"id":"w2","x":640,"y":0,"w":640,"h":900,"xid":"0x124"}]}`
	r.geom = map[string]string{"291": "WINDOW=291\nX=0\nY=0\nWIDTH=640\nHEIGHT=900\nSCREEN=0\n", "292": "WINDOW=292\nX=640\nY=0\nWIDTH=640\nHEIGHT=900\nSCREEN=0\n"}
	c, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 2})
	if e != nil {
		t.Fatal(e)
	}
	action, e := tool.Input(context.Background(), c.Container, "w2", inject.Request{Kind: inject.KindClick, X: 1, Y: 2, Button: 1})
	if e != nil || !action.FocusVerified {
		t.Fatal(action, e)
	}
	found := false
	for _, a := range r.calls {
		if strings.Contains(strings.Join(a, " "), "mousemove 640 2 mousemove --sync 641 2 click") {
			found = true
		}
	}
	if !found {
		t.Fatal("local coordinates were not offset")
	}
	r.pair = pairFixture(Width, Height)
	f, e := tool.Frame(context.Background(), c.Container, "w2")
	if e != nil || f.Width != 640 || f.Height != 900 || s.w != 640 || len(s.rgb) != 640*900*3 || f.Window.X != 640 {
		t.Fatal(f, e)
	}
}

func TestStartClearsStaleBrowserLocksBeforeLaunch(t *testing.T) {
	tool, r, _, _ := setup(t)
	_ = start(t, tool)
	clear, launch := -1, -1
	for i, a := range r.calls {
		s := strings.Join(a, " ")
		if strings.Contains(s, "--entrypoint /bin/rm") && strings.Contains(s, "/profile/SingletonLock") {
			clear = i
			if !strings.Contains(s, "--network none") || !strings.Contains(s, "--cap-drop ALL") || !strings.Contains(s, "dst=/profile") {
				t.Fatal("cleanup must be isolated", s)
			}
		}
	}
	for i, a := range r.calls {
		if strings.Contains(strings.Join(a, " "), "srf-idnet") && clear >= 0 && i > clear {
			launch = i
			break
		}
	}
	if clear < 0 || launch < 0 {
		t.Fatal("stale lock cleanup missing or not before launch", clear, launch)
	}
}

func TestBrowserTilesRespectChromiumMinimum(t *testing.T) {
	tool, r, _, _ := setup(t)
	r.ready = `IDENTITY_READY {"windows":[{"id":"w1","x":0,"y":0,"w":640,"h":450,"xid":"0x123"},{"id":"w2","x":640,"y":0,"w":640,"h":450,"xid":"0x124"},{"id":"w3","x":0,"y":450,"w":640,"h":450,"xid":"0x125"}]}`
	r.geom = map[string]string{
		"291": "WINDOW=291\nX=0\nY=0\nWIDTH=640\nHEIGHT=450\nSCREEN=0\n",
		"292": "WINDOW=292\nX=640\nY=0\nWIDTH=640\nHEIGHT=450\nSCREEN=0\n",
		"293": "WINDOW=293\nX=0\nY=450\nWIDTH=640\nHEIGHT=450\nSCREEN=0\n",
	}
	c, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 3})
	if e != nil {
		t.Fatal(e)
	}
	for _, w := range c.Windows {
		if w.W < BrowserMinWidth || w.H < BrowserMinHeight {
			t.Fatal("tile below Chromium minimum", w)
		}
	}
	tool2, _, _, _ := setup(t)
	if _, e := tool2.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test2", Windows: 5}); e == nil {
		t.Fatal("5 windows cannot fit 640x450 tiles in 1280x900")
	}
}

// These byte goldens were first run against the unchanged browser implementation.
func TestBrowserWireGoldens(t *testing.T) {
	tool, _, _, _ := setup(t)
	c := start(t, tool)
	b, err := json.Marshal(c)
	want := `{"container":"srf-id-test","run_id":"srf-id-test","windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"title":"","url":""}]}`
	if err != nil || string(b) != want {
		t.Fatalf("start wire: %s (%v)", b, err)
	}
	requests := []inject.Request{
		{Kind: inject.KindKey, Keysym: "Tab"},
		{Kind: inject.KindClick, X: 10, Y: 20, Button: 1},
		{Kind: inject.KindText, Text: "synthetic fixture"},
		{Kind: inject.KindScroll, X: 10, Y: 20, Button: 5, Count: 2},
		{Kind: inject.KindDrag, X: 10, Y: 20, X2: 30, Y2: 40, Button: 1},
	}
	for _, req := range requests {
		t.Run(string(req.Kind), func(t *testing.T) {
			a, e := tool.Input(context.Background(), c.Container, "w1", req)
			if e != nil {
				t.Fatal(e)
			}
			b, e := json.Marshal(a)
			want := `{"surface_id":"w1","kind":"` + string(req.Kind) + `","input_mono_ns":42,"focus_verified":true,"result":"ok"}`
			if e != nil || string(b) != want {
				t.Fatalf("input wire: %s (%v)", b, e)
			}
		})
	}
}

func programSpec(t *testing.T, capsule string) StartSpec {
	t.Helper()
	var s StartSpec
	if e := json.Unmarshal([]byte(`{"Identity":"program-a","Kind":"program","Program":"notepad","Capsule":"`+capsule+`","Windows":1}`), &s); e != nil {
		t.Fatal(e)
	}
	return s
}
func programTool(t *testing.T) (*Tool, *fakeRunner, *fakeLaunch) {
	t.Helper()
	tool, r, l, _ := setup(t)
	if e := tool.Store.Create(store.Identity{ProfileID: "program-a", Browser: "program", AccountLabel: "test program", AllowedDomains: []string{"example.org"}}); e != nil {
		t.Fatal(e)
	}
	tool.Proxy = ""
	r.proxy = false
	r.ready = `PROGRAM_READY {"windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"xid":"0x123"}]}`
	return tool, r, l
}

func TestProgramFixedGeometry(t *testing.T) {
	for _, kind := range []string{"program", "vm"} {
		for _, tc := range []struct {
			name                      string
			width, height, max, count int
			code                      int
		}{
			{"fixed", Width, 720, 1, 1, 0},
			{"tiled", 0, 0, 4, 1, 0},
			{"width-only", Width, 0, 1, 1, 2},
			{"height-only", 0, 720, 1, 1, 2},
			{"wide", Width + 1, 720, 1, 1, 2},
			{"tall", Width, Height + 1, 1, 1, 2},
			{"narrow", 399, 720, 1, 1, 2},
			{"short", Width, 299, 1, 1, 2},
			{"catalog-multi", Width, 720, 2, 1, 2},
			{"request-multi", Width, 720, 1, 2, 2},
		} {
			t.Run(kind+"/"+tc.name, func(t *testing.T) {
				tool, r, l := vmTool(t)
				spec := programSpec(t, "srf-pg-fixed")
				if kind == "vm" {
					spec = vmSpec()
				}
				spec.Windows = tc.count
				p, err := programsurface.Lookup(spec.Program)
				if err != nil {
					t.Fatal(err)
				}
				p.FixedWidth, p.FixedHeight, p.MaxWindows = tc.width, tc.height, tc.max
				tool.lookupProgram = func(string) (programsurface.Program, error) { return p, nil }
				height := tc.height
				if height == 0 {
					height = Height
				}
				r.ready = fmt.Sprintf(`PROGRAM_READY {"windows":[{"id":"w1","x":0,"y":0,"w":%d,"h":%d,"xid":"0x123"}]}`, Width, height)
				r.geom = map[string]string{"291": fmt.Sprintf("WINDOW=291\nX=0\nY=0\nWIDTH=%d\nHEIGHT=%d\nSCREEN=0\n", Width, height)}
				c, err := tool.Start(context.Background(), spec)
				if Code(err) != tc.code {
					t.Fatalf("code = %d, want %d: %v", Code(err), tc.code, err)
				}
				if tc.code != 0 {
					if len(l.plans) != 0 || len(r.calls) != 0 {
						t.Fatal("invalid geometry reached runner or launcher")
					}
					return
				}
				wire, err := json.Marshal(c.Windows)
				want := fmt.Sprintf(`[{"id":"w1","x":0,"y":0,"w":%d,"h":%d,"title":"","url":""}]`, Width, height)
				if err != nil || string(wire) != want {
					t.Fatalf("windows = %s: %v; want %s", wire, err, want)
				}
				tile := fmt.Sprintf(`SRF_TILES=[{"id":"w1","x":0,"y":0,"w":%d,"h":%d}]`, Width, height)
				if len(l.plans) != 1 || !slices.Contains(l.plans[0].Argv, tile) {
					t.Fatalf("launch did not contain %s", tile)
				}
				if err := tool.Stop(context.Background(), c.Container); err != nil {
					t.Fatal(err)
				}
			})
		}
	}
}

func TestBrowserStartDoesNotReadProgramGeometry(t *testing.T) {
	tool, _, _, _ := setup(t)
	tool.lookupProgram = func(string) (programsurface.Program, error) {
		t.Fatal("browser consulted program catalog")
		return programsurface.Program{}, nil
	}
	c := start(t, tool)
	if err := tool.Stop(context.Background(), c.Container); err != nil {
		t.Fatal(err)
	}
}

func TestProgramNetworkNoneLifecycle(t *testing.T) {
	for _, failed := range []bool{false, true} {
		t.Run(fmt.Sprint(failed), func(t *testing.T) {
			tool, r, l := programTool(t)
			// A pre-existing unrelated registry record must survive start/failure/stop byte-for-byte.
			sentinel := []byte(`{"srf-id-other":"172.30.80.44"}`)
			if e := os.WriteFile(tool.Registry, sentinel, 0600); e != nil {
				t.Fatal(e)
			}
			l.fail = failed
			c, e := tool.Start(context.Background(), programSpec(t, "srf-pg-test"))
			if failed {
				if Code(e) != 6 {
					t.Fatal(e)
				}
			} else {
				if e != nil {
					t.Fatal(e)
				}
				b, _ := json.Marshal(c)
				if !strings.Contains(string(b), `"kind":"program"`) {
					t.Fatal("kind missing")
				}
				args := strings.Join(l.plans[0].Argv, " ")
				if !strings.Contains(args, "--network none") || !strings.Contains(args, "SRF_PROGRAM=notepad") || strings.Contains(args, "--security-opt") || strings.Contains(args, "--ip ") || strings.Contains(args, "SRF_EGRESS_PROXY") {
					t.Fatal("program argv", args)
				}
				before := len(r.calls)
				if _, e = tool.AX(context.Background(), c.Container, "w1", ""); Code(e) != 4 || e.Error() != "AUSENTE — program ax arrives in P2" {
					t.Fatal(e)
				}
				if len(r.calls) != before {
					t.Fatal("program AX executed runner")
				}
			}
			for _, a := range r.calls {
				s := strings.Join(a, " ")
				if strings.Contains(s, "inspect") || strings.Contains(s, "Singleton") {
					t.Fatal("program touched browser control", s)
				}
			}
			if e = tool.Stop(context.Background(), "srf-pg-test"); e != nil {
				t.Fatal(e)
			}
			b, e := os.ReadFile(tool.Registry)
			if e != nil || string(b) != string(sentinel) {
				t.Fatal("registry changed")
			}
			if _, e = os.Lstat(tool.Registry + ".lock"); !os.IsNotExist(e) {
				t.Fatal("registry was locked")
			}
		})
	}
}
func TestProgramNetworkNoneDoesNotCreateRegistry(t *testing.T) {
	tool, _, _ := programTool(t)
	if _, e := tool.Start(context.Background(), programSpec(t, "srf-pg-test")); e != nil {
		t.Fatal(e)
	}
	if _, e := os.Lstat(tool.Registry); !os.IsNotExist(e) {
		t.Fatal("registry created")
	}
	if e := tool.Stop(context.Background(), "srf-pg-test"); e != nil {
		t.Fatal(e)
	}
	if _, e := os.Lstat(tool.Registry); !os.IsNotExist(e) {
		t.Fatal("registry created on stop")
	}
}
func TestProgramAndBrowserIdentityIsolation(t *testing.T) {
	tool, r, _ := programTool(t)
	tool.Proxy = ProxyURL
	r.proxy = true
	if _, e := tool.Start(context.Background(), StartSpec{Identity: "program-a", Capsule: "srf-id-test", Windows: 1}); Code(e) != 4 {
		t.Fatal(e)
	}
	spec := programSpec(t, "srf-pg-test")
	spec.Identity = "identity-a"
	if _, e := tool.Start(context.Background(), spec); Code(e) != 4 {
		t.Fatal(e)
	}
}

func TestOldSessionDefaultsToBrowser(t *testing.T) {
	tool, _, _, _ := setup(t)
	c := start(t, tool)
	b, e := readPrivate(tool.statePath(c.Container), 1<<20)
	if e != nil {
		t.Fatal(e)
	}
	var state map[string]any
	if e = json.Unmarshal(b, &state); e != nil {
		t.Fatal(e)
	}
	delete(state, "kind")
	delete(state, "program")
	if e = atomicJSON(tool.statePath(c.Container), state); e != nil {
		t.Fatal(e)
	}
	s, e := tool.load(c.Container)
	if e != nil || s.Kind != "browser" || s.Program != "" {
		t.Fatal("legacy session", e)
	}
}

func TestDownloadsFolderArgumentPerKind(t *testing.T) {
	for _, kind := range []string{"browser", "program"} {
		t.Run(kind, func(t *testing.T) {
			var tool *Tool
			var r *fakeRunner
			var c Capsule
			var e error
			folder := "/profile/Downloads"
			if kind == "program" {
				tool, r, _ = programTool(t)
				c, e = tool.Start(context.Background(), programSpec(t, "srf-pg-test"))
				folder = "/profile/exchange"
			} else {
				tool, r, _, _ = setup(t)
				c = start(t, tool)
			}
			if e != nil {
				t.Fatal(e)
			}
			for _, tail := range [][]string{nil, {"note.txt", "15728640"}, {"note.txt", "delete"}} {
				if _, e = tool.downloadsRun(context.Background(), c.Container, tail...); e != nil {
					t.Fatal(e)
				}
				a := r.calls[len(r.calls)-1]
				i := slices.Index(a, downloadsPython)
				if i < 0 || a[i+1] != folder || !slices.Equal(a[i+2:], tail) {
					t.Fatal("helper folder/args")
				}
			}
		})
	}
}

func TestProgramFrameDoesNotDeriveURLFromTitle(t *testing.T) {
	tool, r, _ := programTool(t)
	c, e := tool.Start(context.Background(), programSpec(t, "srf-pg-test"))
	if e != nil {
		t.Fatal(e)
	}
	r.pair = pairFixture(Width, Height)
	f, e := tool.Frame(context.Background(), c.Container, "w1")
	if e != nil {
		t.Fatal(e)
	}
	if f.Window.URL != "" || f.Window.Title == "" {
		t.Fatal("program title/url", f.Window)
	}
}

func TestProgramLookupCodesAndWindowLimit(t *testing.T) {
	for _, id := range []string{"unknown", "mousepad"} {
		tool, _, _ := programTool(t)
		s := programSpec(t, "srf-pg-test")
		s.Program = id
		if _, e := tool.Start(context.Background(), s); Code(e) != 2 {
			t.Fatal(id, e)
		}
	}
	tool, r, l := programTool(t)
	s := programSpec(t, "srf-pg-test")
	s.Windows = 5
	if _, e := tool.Start(context.Background(), s); Code(e) != 2 || len(r.calls) != 0 || len(l.plans) != 0 {
		t.Fatal("window limit", e)
	}
}

func TestProgramGeneratedNameAndReadinessRefusal(t *testing.T) {
	tool, _, _ := programTool(t)
	c, e := tool.Start(context.Background(), programSpec(t, ""))
	if e != nil {
		t.Fatal(e)
	}
	if !strings.HasPrefix(c.Container, "srf-pg-") || len(c.Container) != len("srf-pg-")+12 {
		t.Fatal("generated program capsule name")
	}
	if e = tool.Stop(context.Background(), c.Container); e != nil {
		t.Fatal(e)
	}
	tool, r, _ := programTool(t)
	r.ready = `PROGRAM_READY {"windows":[{"id":"w1","x":1,"y":0,"w":1280,"h":900,"xid":"0x123"}]}`
	if _, e = tool.Start(context.Background(), programSpec(t, "srf-pg-invalid")); Code(e) != 6 {
		t.Fatal(e)
	}
	if e = tool.Stop(context.Background(), "srf-pg-invalid"); e != nil {
		t.Fatal(e)
	}
	if _, e = os.Lstat(tool.Registry); !os.IsNotExist(e) {
		t.Fatal("registry touched during readiness failure/stop")
	}
}

// The catalog may change between CLI invocations. All later verbs must use
// the private launch snapshot, even when that catalog entry is now disabled.
func TestProgramSurvivesCatalogDisable(t *testing.T) {
	tool, r, l := programTool(t)
	ctx := context.Background()
	c, err := tool.Start(ctx, programSpec(t, "srf-pg-test"))
	if err != nil {
		t.Fatal(err)
	}
	lookups := 0
	tool.lookupProgram = func(string) (programsurface.Program, error) {
		lookups++
		return programsurface.Program{}, &programsurface.Error{Code: 2, Detail: "AUSENTE — disabled test entry"}
	}
	r.pair = pairFixture(Width, Height)
	if _, err = tool.Frame(ctx, c.Container, "w1"); err != nil {
		t.Error("frame", err)
	}
	for _, req := range []inject.Request{
		{Kind: inject.KindKey, Keysym: "Tab"}, {Kind: inject.KindClick, X: 10, Y: 20, Button: 1},
		{Kind: inject.KindText, Text: "synthetic fixture"}, {Kind: inject.KindScroll, X: 10, Y: 20, Button: 5, Count: 2},
		{Kind: inject.KindDrag, X: 10, Y: 20, X2: 30, Y2: 40, Button: 1},
	} {
		if _, err = tool.Input(ctx, c.Container, "w1", req); err != nil {
			t.Error(req.Kind, err)
		}
	}
	if _, err = tool.Paste(ctx, c.Container, "w1", inject.Request{Kind: inject.KindText, Text: "synthetic fixture"}); err != nil {
		t.Error("paste", err)
	}
	if _, err = tool.AX(ctx, c.Container, "w1", ""); Code(err) != 4 {
		t.Error("ax", err)
	}
	// Exercise listing through the same public verb used by the CLI.
	if _, err = tool.Downloads(ctx, c.Container); err != nil {
		t.Error("downloads", err)
	}
	tool.jqueryPath = filepath.Join(t.TempDir(), "jquery.js")
	if err = os.WriteFile(tool.jqueryPath, []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	tool.Listen = func(context.Context, string, string) (net.Listener, error) {
		return &pipeListener{conns: make(chan net.Conn), closed: make(chan struct{})}, nil
	}
	viewCtx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	announced := false
	if err = tool.ViewAt(viewCtx, c.Container, 0, true, func(v ViewInfo) error {
		announced = true
		if v.Socket != tool.viewSocket(c.Container) {
			t.Error("view socket")
		}
		cancel()
		return nil
	}); err != nil {
		t.Error("view", err)
	}
	if !announced {
		t.Error("view not announced")
	}
	if _, err = tool.ViewStop(ctx, c.Container); Code(err) != 4 {
		t.Error("closed view-stop", err)
	}
	// Force readonly restoration to fail and prove program views use xpra-stop.
	tool.R = &viewModeRunner{base: r, failures: 2}
	viewCtx2, cancel2 := context.WithTimeout(ctx, time.Second)
	defer cancel2()
	if err = tool.ViewAt(viewCtx2, c.Container, 0, true, func(ViewInfo) error { cancel2(); return nil }); Code(err) != 6 {
		t.Error("view-stop fallback", err)
	}
	tool.R = r
	joined := ""
	for _, a := range r.calls {
		joined += strings.Join(a, " ") + "\n"
	}
	for _, want := range []string{"/profile/exchange", "SELECTION_CLIPBOARD", "xpra-stop", "SRF_LOGIN=1", "SRF_LOGIN=0", "xdotool type --clearmodifiers --delay 0 --file -"} {
		if !strings.Contains(joined, want) {
			t.Error("missing transport", want)
		}
	}
	if err = tool.Stop(ctx, c.Container); err != nil {
		t.Error("stop", err)
	}
	if l.removes != 1 {
		t.Error("capsule not removed")
	}
	if lookups != 0 {
		t.Error("later verbs consulted live catalog")
	}
	if _, err = os.Stat(tool.Registry + ".lock"); !os.IsNotExist(err) {
		t.Error("registry touched")
	}
	lease, err := tool.Store.Acquire("program-a", "other")
	if err != nil {
		t.Error("lease retained")
	} else {
		_ = tool.Store.Release(lease)
	}
}

func TestProgramReadinessSeparator(t *testing.T) {
	for _, line := range []string{"PROGRAM_READY", "PROGRAM_READY\t{}", "PROGRAM_READY{}", "PROGRAM_READY  {}"} {
		t.Run(line, func(t *testing.T) {
			tool, r, _ := programTool(t)
			r.ready = line
			ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
			defer cancel()
			_, err := tool.Start(ctx, programSpec(t, "srf-pg-test"))
			if Code(err) != 6 {
				t.Fatalf("malformed report code=%d", Code(err))
			}
			logs := 0
			for _, a := range r.calls {
				if len(a) > 1 && a[1] == "logs" {
					logs++
				}
			}
			if logs != 1 {
				t.Error("malformed report did not fail immediately")
			}
		})
	}
}

func TestProgramNilLaunchArguments(t *testing.T) {
	tool, _, l := programTool(t)
	tool.programRunArgs = func(string, string, string, programsurface.Program, []programsurface.Tile, string) []string {
		return nil
	}
	_, err := tool.Start(context.Background(), programSpec(t, "srf-pg-test"))
	if Code(err) != 3 || err.Error() != "program launch arguments refused" {
		t.Fatalf("code=%d detail=%v", Code(err), err)
	}
	if len(l.plans) != 0 {
		t.Error("refused argv launched")
	}
	if err = tool.Stop(context.Background(), "srf-pg-test"); err != nil {
		t.Fatal(err)
	}
}

func TestProgramMissingLaunchDecisionRefused(t *testing.T) {
	for _, field := range []string{"networked", "seccomp_identity", "clear_singleton"} {
		for _, null := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s-null-%t", field, null), func(t *testing.T) {
				tool, _, _ := programTool(t)
				c, err := tool.Start(context.Background(), programSpec(t, "srf-pg-test"))
				if err != nil {
					t.Fatal(err)
				}
				raw, err := os.ReadFile(tool.statePath(c.Container))
				if err != nil {
					t.Fatal(err)
				}
				var state map[string]any
				if err = json.Unmarshal(raw, &state); err != nil {
					t.Fatal(err)
				}
				snapshot, ok := state["program_launch"].(map[string]any)
				if !ok {
					t.Fatal("missing launch snapshot")
				}
				if null {
					snapshot[field] = nil
				} else {
					delete(snapshot, field)
				}
				raw, err = json.Marshal(state)
				if err != nil {
					t.Fatal(err)
				}
				if err = os.WriteFile(tool.statePath(c.Container), raw, 0600); err != nil {
					t.Fatal(err)
				}
				if _, err = tool.load(c.Container); Code(err) != 6 {
					t.Error("absent decision accepted", err)
				}
			})
		}
	}
}

func TestVMStartSpecNames(t *testing.T) {
	for _, tc := range []struct {
		kind, program, capsule string
		code                   int
	}{
		{"vm", "notepad-vm", "srf-vm-test", 0}, {"vm", "notepad-vm", "", 0},
		{"vm", "", "srf-vm-test", 2}, {"vm", "notepad-vm", "srf-pg-test", 2},
		{"program", "notepad", "srf-vm-test", 2}, {"browser", "", "srf-vm-test", 2},
		{"vm", "notepad-vm", "srf-vm-", 2}, {"vm", "notepad-vm", "srf-vm-UPPER", 2},
	} {
		e := ValidateStartSpec(StartSpec{Identity: "program-a", Kind: tc.kind, Program: tc.program, Capsule: tc.capsule, Windows: 1})
		if Code(e) != tc.code {
			t.Errorf("%+v: %v", tc, e)
		}
	}
}

func vmTool(t *testing.T) (*Tool, *fakeRunner, *fakeLaunch) {
	t.Helper()
	tool, r, l := programTool(t)
	tool.vmHostRead = func(path string) ([]byte, error) {
		switch path {
		case "/etc/group":
			return []byte("other:x:11:\nkvm:x:109:\n"), nil
		case "/proc/meminfo":
			return []byte("MemAvailable: 16777216 kB\n"), nil
		}
		return nil, os.ErrNotExist
	}
	return tool, r, l
}
func vmSpec() StartSpec {
	return StartSpec{Kind: "vm", Program: "notepad-vm", Identity: "program-a", Capsule: "srf-vm-test", Windows: 1}
}
func TestVMLifecycleWireAndProgramBehavior(t *testing.T) {
	tool, r, l := vmTool(t)
	t.Setenv("SRF_KVM_GID", "unchanged")
	c, e := tool.Start(context.Background(), vmSpec())
	if e != nil {
		t.Fatal(e)
	}
	b, _ := json.Marshal(c)
	want := `{"kind":"vm","container":"srf-vm-test","run_id":"srf-vm-test","windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"title":"","url":""}]}`
	if string(b) != want {
		t.Fatal("start wire", string(b))
	}
	args := l.plans[0].Argv
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--device /dev/kvm --group-add 109") || !strings.Contains(joined, "SRF_PROGRAM=notepad-vm") {
		t.Fatal("VM argv", joined)
	}
	if os.Getenv("SRF_KVM_GID") != "unchanged" {
		t.Fatal("environment override leaked")
	}
	if _, e = tool.AX(context.Background(), c.Container, "w1", ""); Code(e) != 4 || !strings.HasPrefix(e.Error(), "AUSENTE") {
		t.Fatal("VM AX", e)
	}
	if _, e = tool.downloadsRun(context.Background(), c.Container); e != nil {
		t.Fatal(e)
	}
	a := r.calls[len(r.calls)-1]
	i := slices.Index(a, downloadsPython)
	if i < 0 || a[i+1] != "/profile/exchange" {
		t.Fatal("VM downloads folder")
	}
	r.pair = pairFixture(Width, Height)
	frame, e := tool.Frame(context.Background(), c.Container, "w1")
	if e != nil || frame.Window.URL != "" {
		t.Fatal("VM frame", e)
	}
	stateBytes, e := readPrivate(tool.statePath(c.Container), 1<<20)
	var state session
	if e != nil || json.Unmarshal(stateBytes, &state) != nil || state.Kind != "vm" || state.Capsule.Kind != "vm" {
		t.Fatal("VM kind not preserved", e)
	}
	tool.lookupProgram = func(string) (programsurface.Program, error) {
		t.Fatal("stop consulted catalog")
		return programsurface.Program{}, nil
	}
	if e = tool.Stop(context.Background(), c.Container); e != nil {
		t.Fatal(e)
	}
	if _, e = os.Lstat(tool.Registry); !os.IsNotExist(e) {
		t.Fatal("VM touched registry")
	}
	for _, a := range r.calls {
		s := strings.Join(a, " ")
		if strings.Contains(s, "inspect") || strings.Contains(s, "Singleton") {
			t.Fatal("browser control used", s)
		}
	}
}
func TestVMRuntimeMismatchRefusedBeforeLaunch(t *testing.T) {
	for _, kind := range []string{"program", "vm"} {
		tool, r, l := vmTool(t)
		spec := vmSpec()
		if kind == "program" {
			spec.Kind = "program"
			spec.Capsule = "srf-pg-test"
		} else {
			spec.Program = "notepad"
		}
		_, e := tool.Start(context.Background(), spec)
		if Code(e) != 2 || len(l.plans) != 0 || len(r.calls) != 0 {
			t.Fatal(kind, e)
		}
	}
}
func TestVMHostAdmission(t *testing.T) {
	for _, tc := range []struct {
		group, mem string
		code       int
		message    string
	}{
		{"kvm:x:109:", "MemAvailable: 16777216 kB", 0, ""},
		{"kvm:x:1:", "MemAvailable: 16777216 kB", 0, ""},
		{"kvm:x:4294967294:", "MemAvailable: 16777216 kB", 0, ""},
		{"other:x:109:", "MemAvailable: 16777216 kB", 3, "kvm group unavailable"},
		{"kvm:x:bad:", "MemAvailable: 16777216 kB", 3, "kvm group gid invalid"},
		{"kvm:x:0:", "MemAvailable: 16777216 kB", 3, "kvm group gid invalid"},
		{"kvm:x:-1:", "MemAvailable: 16777216 kB", 3, "kvm group gid invalid"},
		{"kvm:x:4294967295:", "MemAvailable: 16777216 kB", 3, "kvm group gid invalid"},
		{"kvm:x:4294967296:", "MemAvailable: 16777216 kB", 3, "kvm group gid invalid"},
		{"kvm:x:109:\nkvm:x:110:", "MemAvailable: 16777216 kB", 3, "invalid kvm group"},
		{"kvm:x:109:", "MemAvailable: 16777215 kB", 3, "VM requires MemAvailable >= 16 GiB"},
		{"kvm:x:109:", "MemTotal: 99999999 kB", 3, "VM requires MemAvailable >= 16 GiB"},
		{"kvm:x:109:", "MemAvailable: invalid kB", 3, "VM requires MemAvailable >= 16 GiB"},
		{"kvm:x:109:", "MemAvailable: 16777216 MB", 3, "VM requires MemAvailable >= 16 GiB"},
	} {
		t.Run(tc.group+"/"+tc.mem, func(t *testing.T) {
			tool, r, l := vmTool(t)
			tool.vmHostRead = func(path string) ([]byte, error) {
				if path == "/etc/group" {
					return []byte(tc.group), nil
				}
				return []byte(tc.mem), nil
			}
			_, e := tool.Start(context.Background(), vmSpec())
			if Code(e) != tc.code {
				t.Fatalf("code = %d, want %d: %v", Code(e), tc.code, e)
			}
			if tc.code != 0 {
				if e.Error() != tc.message {
					t.Errorf("error = %q, want %q", e.Error(), tc.message)
				}
				if len(l.plans) != 0 || len(r.calls) != 0 {
					t.Fatal("admission effects", e)
				}
			}
		})
	}
	for _, tc := range []struct{ path, message string }{
		{"/etc/group", "kvm group unavailable"},
		{"/proc/meminfo", "VM memory admission unavailable"},
	} {
		t.Run(tc.path, func(t *testing.T) {
			tool, r, l := vmTool(t)
			tool.vmHostRead = func(path string) ([]byte, error) {
				if path == tc.path {
					return nil, os.ErrNotExist
				}
				return []byte("kvm:x:109:"), nil
			}
			_, e := tool.Start(context.Background(), vmSpec())
			if Code(e) != 3 || e.Error() != tc.message || len(l.plans) != 0 || len(r.calls) != 0 {
				t.Fatal("admission read failure", e)
			}
		})
	}
}

type delayedReadyRunner struct {
	base         *fakeRunner
	polls, after int
	tail         string
}

func (r *delayedReadyRunner) Run(ctx context.Context, a []string, in []byte) ([]byte, error) {
	if len(a) > 1 && a[1] == "logs" {
		r.polls++
		if len(a) != 5 || a[3] != r.tail {
			return nil, fmt.Errorf("wrong log tail: %v", a)
		}
		if r.polls <= r.after {
			return []byte("booting\n"), nil
		}
	}
	return r.base.Run(ctx, a, in)
}
func TestVMReadinessBudgetOnlyForVM(t *testing.T) {
	for _, kind := range []string{"browser", "program", "vm"} {
		t.Run(kind, func(t *testing.T) {
			tool, base, _ := vmTool(t)
			spec := vmSpec()
			spec.Kind = kind
			attempts, tail := 100, "30"
			if kind == "vm" {
				attempts, tail = 600, "60"
			} else if kind == "program" {
				spec.Program = "notepad"
				spec.Capsule = "srf-pg-test"
			} else {
				tool.Proxy = ProxyURL
				base.proxy = true
				base.ready = ""
				spec.Program = ""
				spec.Capsule = "srf-id-test"
				spec.Identity = "identity-a"
			}
			r := &delayedReadyRunner{base: base, after: attempts - 1, tail: tail}
			tool.R = r
			waits := 0
			tool.readinessWait = func(context.Context) error { waits++; return nil }
			if _, e := tool.Start(context.Background(), spec); e != nil {
				t.Fatal(e)
			}
			if r.polls != attempts || waits != attempts-1 {
				t.Fatal("budget", r.polls, waits)
			}
			// A peer that never becomes ready must stop at precisely this kind's budget.
			tool, base, _ = vmTool(t)
			if kind == "browser" {
				tool.Proxy = ProxyURL
				base.proxy = true
				base.ready = ""
			}
			r = &delayedReadyRunner{base: base, after: attempts, tail: tail}
			tool.R = r
			tool.readinessWait = func(context.Context) error { return nil }
			if _, e := tool.Start(context.Background(), spec); Code(e) != 4 || r.polls != attempts {
				t.Fatal("unbounded readiness", e, r.polls)
			}
		})
	}
}
func TestVMGeneratedNameAndSessionKindTamper(t *testing.T) {
	tool, _, _ := vmTool(t)
	spec := vmSpec()
	spec.Capsule = ""
	c, e := tool.Start(context.Background(), spec)
	if e != nil || !regexp.MustCompile(`^srf-vm-[0-9a-f]{12}$`).MatchString(c.Container) {
		t.Fatal(c, e)
	}
	state, e := tool.load(c.Container)
	if e != nil {
		t.Fatal(e)
	}
	state.Kind = "browser"
	if e = tool.save(state); e != nil {
		t.Fatal(e)
	}
	if _, e = tool.load(c.Container); Code(e) != 6 {
		t.Fatal("accepted altered VM state", e)
	}
}

func TestVMRequiresProgramReadyAndWaitHonorsCancellation(t *testing.T) {
	tool, r, _ := vmTool(t)
	r.ready = `IDENTITY_READY {"windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"xid":"0x123"}]}`
	waits := 0
	tool.readinessWait = func(context.Context) error { waits++; return nil }
	if _, e := tool.Start(context.Background(), vmSpec()); Code(e) != 4 || waits != 600 {
		t.Fatal(e, waits)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if e := (Tool{}).waitReadiness(ctx); Code(e) != 6 {
		t.Fatal(e)
	}
}
func TestContainerDoesNotUseKVMAdmissionOrFlags(t *testing.T) {
	tool, _, l := programTool(t)
	tool.vmHostRead = func(string) ([]byte, error) { t.Fatal("container used VM admission"); return nil, os.ErrNotExist }
	if _, e := tool.Start(context.Background(), programSpec(t, "srf-pg-test")); e != nil {
		t.Fatal(e)
	}
	for _, arg := range l.plans[0].Argv {
		if arg == "--device" || arg == "--group-add" || arg == "/dev/kvm" {
			t.Fatal("VM flag on container", arg)
		}
	}
}
