package capsule

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

type hostWindowFakeRunner struct {
	calls      [][]string
	windows    map[string]*HostWindowSpec
	captureOut []byte
	captureErr error
	inputOut   []byte
	inputErr   error
	listOut    []byte
	onList     func()
	otherOut   []byte
	otherErr   error
}

func (f *hostWindowFakeRunner) ConsumeOpeningWindow(profile string) (*HostWindowSpec, error) {
	if f.windows == nil {
		return nil, nil
	}
	spec := f.windows[profile]
	delete(f.windows, profile)
	if spec == nil {
		return nil, nil
	}
	if err := ValidateHostWindowSpec(spec); err != nil {
		return nil, err
	}
	return spec, nil
}

func (f *hostWindowFakeRunner) Run(_ context.Context, argv []string, _ []byte) ([]byte, error) {
	if len(argv) == 0 {
		return nil, nil
	}
	call := append([]string{filepath.Base(argv[0])}, argv[1:]...)
	f.calls = append(f.calls, call)
	if filepath.Base(argv[0]) == "srf-host-window" && len(argv) > 1 {
		switch argv[1] {
		case "list":
			if f.onList != nil {
				f.onList()
			}
			return f.listOut, nil
		case "capture":
			return f.captureOut, f.captureErr
		case "input":
			if f.inputOut == nil && f.inputErr == nil {
				fields := map[string]string{}
				for i := 2; i+1 < len(argv); i++ {
					fields[argv[i]] = argv[i+1]
				}
				kind := "click"
				if fields["--key"] != "" {
					kind = "key"
				}
				if fields["--type"] != "" {
					kind = "type"
				}
				xid, _ := strconv.ParseUint(fields["--xid"], 10, 32)
				out, _ := json.Marshal(map[string]any{"ok": true, "xid": xid, "witness": fields["--witness"], "kind": kind, "focus_verified": true})
				return out, nil
			}
			return f.inputOut, f.inputErr
		}
	}
	return f.otherOut, f.otherErr
}

func makeTestXWD(w, h int, pixel byte) []byte {
	b := make([]byte, 100+4*w*h)
	fields := map[int]uint32{
		0: 100, 1: 7, 2: 2, 3: 24,
		4: uint32(w), 5: uint32(h),
		11: 32, 12: uint32(4 * w), 13: 4,
		14: 0xff0000, 15: 0xff00, 16: 0xff,
	}
	for i, v := range fields {
		binary.BigEndian.PutUint32(b[i*4:], v)
	}
	for i := 100; i < len(b); i += 4 {
		b[i], b[i+1], b[i+2] = pixel, pixel+1, pixel+2
	}
	return b
}

func ownedHostXpraFixture(t *testing.T) *hostXpraProcess {
	t.Helper()
	root, err := os.MkdirTemp("/dev/shm", "mv-host-xpra-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	anchor := exec.Command("sleep", "30")
	anchor.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := anchor.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = anchor.Process.Kill(); _ = anchor.Wait() })
	p := &hostXpraProcess{
		root: root, display: ":12345", authority: filepath.Join(root, "Xauthority"), anchor: anchor,
		ownsSocket: func(pid int, display string) bool { return pid == anchor.Process.Pid && display == ":12345" },
	}
	if err := os.WriteFile(p.authority, []byte("synthetic authority"), 0600); err != nil {
		t.Fatal(err)
	}
	p.authorityInfo, err = os.Lstat(p.authority)
	if err != nil {
		t.Fatal(err)
	}
	p.start = p.processStart(anchor.Process.Pid)
	p.target = hostWindowTarget{77594627, "fixture-window", anchor.Process.Pid, p.start}
	if !p.displayOwned(p.display, p.authority) {
		t.Fatal("offline owned display witness not admitted")
	}
	return p
}

func setHostInventory(r *hostWindowFakeRunner, target hostWindowTarget, w, h int) {
	r.listOut = []byte(fmt.Sprintf(`{"windows":[{"xid":%d,"witness":%q,"pid":%d,"window_type":"normal","visible":true,"rect":{"width":%d,"height":%d}}]}`, target.xid, target.witness, target.pid, w, h))
}

func ownedHostWindowFixture(t *testing.T) (*Manager, *hostWindowFakeRunner, Binding) {
	t.Helper()
	p := ownedHostXpraFixture(t)
	r := &hostWindowFakeRunner{captureOut: makeTestXWD(2, 1, 42)}
	setHostInventory(r, p.target, 2, 1)
	m, err := NewManager(Config{BinDir: t.TempDir(), OriginNS: 100, MaxSurfaces: 2, MaxOutputBytes: 4096, Timeout: time.Second,
		Profiles: map[string]Profile{"demo": {Kind: "browser", NativeKind: "browser", Width: 2, Height: 1, Display: ":100", RAMRoot: p.root}}}, r, func() uint64 { return 200 })
	if err != nil {
		t.Fatal(err)
	}
	profile := Profile{Kind: "desktop_capsule", NativeKind: "host-xpra", RAMRoot: p.root, Width: 2, Height: 1, Display: p.display}
	m.cfg.Profiles["private"] = profile
	spec := &HostWindowSpec{XID: p.target.xid, Witness: p.target.witness, Display: p.display, Xauthority: p.authority}
	if _, err := m.attachOwnedHostWindow("private", "surface-private", "v0.2", profile, spec, Geometry{W: 2, H: 1}, p); err != nil {
		t.Fatal(err)
	}
	return m, r, m.bindings["surface-private"]
}

func hostTestInput(m *Manager, b Binding, proposal string) error {
	params, _ := jsonParams(b.Surface.SurfaceID, proposal, "agent", map[string]any{"type": "key", "keys": []string{"Return"}})
	_, err := m.Handle(context.Background(), req("input", params))
	return err
}

func (r *hostWindowFakeRunner) count(op string) int {
	n := 0
	for _, call := range r.calls {
		if len(call) > 1 && call[1] == op {
			n++
		}
	}
	return n
}

func TestH1NativePreflightNominal(t *testing.T) {
	m, r, b := ownedHostWindowFixture(t)
	if _, _, err := m.Capture(context.Background(), b.Surface.SurfaceID); err != nil {
		t.Fatal(err)
	}
	if err := hostTestInput(m, b, "valid"); err != nil {
		t.Fatal(err)
	}
	if r.count("capture") != 1 || r.count("input") != 1 {
		t.Fatalf("unexpected native call counts: %v", r.calls)
	}
	t.Logf("owned fixture capture=%d input=%d", r.count("capture"), r.count("input"))
}

func TestH1NativePreflightNegative(t *testing.T) {
	for name, change := range map[string]func(*Binding){
		"host":            func(b *Binding) { b.HostDisplay = ":0" },
		"host-screen":     func(b *Binding) { b.HostDisplay = ":0.1" },
		"unset":           func(b *Binding) { b.HostDisplay = "" },
		"unresolved":      func(b *Binding) { b.HostDisplay = "unresolved" },
		"foreign":         func(b *Binding) { b.HostXpra.ownsSocket = func(int, string) bool { return false } },
		"non-private":     func(b *Binding) { b.HostDisplay = ":99" },
		"wrong-authority": func(b *Binding) { b.HostXauthority = filepath.Join(t.TempDir(), "operator-authority") },
		"legacy-fallback": func(b *Binding) { b.NativeKind, b.HostXpra = "host-window", nil },
	} {
		t.Run(name, func(t *testing.T) {
			m, r, b := ownedHostWindowFixture(t)
			id := b.Surface.SurfaceID
			change(&b)
			m.bindings[id] = b
			if _, _, err := m.Capture(context.Background(), id); err == nil {
				t.Fatal("invalid display captured")
			}
			if err := hostTestInput(m, b, "invalid"); err == nil {
				t.Fatal("invalid display received input")
			}
			spec := &HostWindowSpec{XID: b.HostXID, Witness: b.HostWitness, Display: b.HostDisplay, Xauthority: b.HostXauthority}
			if _, err := m.attachHostWindow("staged", "surface-unowned", "v0.2", Profile{}, spec, Geometry{W: 2, H: 1}); err == nil {
				t.Fatal("staged claim supplied native authority")
			}
			if len(r.calls) != 0 {
				t.Fatalf("refusal reached native code: %v", r.calls)
			}
			t.Log("native inventory=0 capture=0 input=0")
		})
	}
}

func TestH1NativeHandoffNominal(t *testing.T) {
	m, r, b := ownedHostWindowFixture(t)
	id := b.Surface.SurfaceID
	before := b.HostXpra.target
	captured, frame, err := m.Capture(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if err := hostTestInput(m, b, "handoff"); err != nil {
		t.Fatal(err)
	}
	after := m.bindings[id]
	if !sameHostWindowBinding(b, captured) || !sameHostWindowBinding(b, after) || after.HostXpra.target != before ||
		frame.Generation != b.Container || r.count("input") != 1 {
		t.Fatal("window or process witness changed during handoff")
	}
	for _, call := range r.calls {
		if call[1] == "input" && (!slices.Contains(call, "--key-hold-ms") || !slices.Contains(call, before.witness)) {
			t.Fatal(call)
		}
	}
	t.Log("attach, capture and input used one process incarnation and generation")
}

func TestH1NativeHandoffNegative(t *testing.T) {
	for name, mutate := range map[string]func(*Manager, *hostWindowFakeRunner, Binding){
		"pid-replaced": func(_ *Manager, r *hostWindowFakeRunner, b Binding) {
			other := ownedHostXpraFixture(t)
			target := b.HostXpra.target
			target.pid = other.target.pid
			setHostInventory(r, target, 2, 1)
		},
		"start-replaced": func(_ *Manager, _ *hostWindowFakeRunner, b Binding) { b.HostXpra.target.start += "-stale" },
		"rebound-xid": func(m *Manager, _ *hostWindowFakeRunner, b Binding) {
			b.HostXID++
			m.bindings[b.Surface.SurfaceID] = b
		},
		"excluded-pid": func(_ *Manager, r *hostWindowFakeRunner, b Binding) {
			target := b.HostXpra.target
			target.pid = os.Getpid()
			setHostInventory(r, target, 2, 1)
		},
	} {
		t.Run(name, func(t *testing.T) {
			m, r, b := ownedHostWindowFixture(t)
			if _, _, err := m.Capture(context.Background(), b.Surface.SurfaceID); err != nil {
				t.Fatal(err)
			}
			mutate(m, r, b)
			if err := hostTestInput(m, b, "stale"); err == nil || r.count("input") != 0 {
				t.Fatalf("changed process/window reached input: %v %v", err, r.calls)
			}
			t.Log("input=0 after window or PID substitution")
		})
	}
}

func TestH1NativeBytesNominal(t *testing.T) {
	m, r, b := ownedHostWindowFixture(t)
	_, f, err := m.Capture(context.Background(), b.Surface.SurfaceID)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(f.Path)
	if err != nil || len(data) != f.Width*f.Height*3 || f.Generation != b.Container ||
		fmt.Sprintf("%x", sha256.Sum256(data)) != f.SHA256RGB24 {
		t.Fatalf("frame evidence mismatch: %+v %v", f, err)
	}
	if err := hostTestInput(m, b, "observed"); err != nil || r.count("input") != 1 {
		t.Fatalf("matching frame did not authorize input: %v %v", err, r.calls)
	}
	t.Log("verified RGB extent, geometry, generation and SHA-256 before input")
}

func TestH1NativeBytesNegative(t *testing.T) {
	for name, mutate := range map[string]func(*Manager, *hostWindowFakeRunner, Binding, NativeFrame){
		"digest": func(_ *Manager, _ *hostWindowFakeRunner, _ Binding, f NativeFrame) {
			if err := os.WriteFile(f.Path, bytes.Repeat([]byte{0}, f.Width*f.Height*3), 0600); err != nil {
				t.Fatal(err)
			}
		},
		"extent": func(_ *Manager, _ *hostWindowFakeRunner, _ Binding, f NativeFrame) {
			if err := os.Truncate(f.Path, 0); err != nil {
				t.Fatal(err)
			}
		},
		"geometry": func(m *Manager, _ *hostWindowFakeRunner, b Binding, _ NativeFrame) {
			m.UpdateHostWindowGeometry(b.Surface.SurfaceID, 3, 1)
			m.UpdateHostWindowGeometry(b.Surface.SurfaceID, 2, 1)
		},
		"native-geometry": func(_ *Manager, r *hostWindowFakeRunner, b Binding, _ NativeFrame) {
			setHostInventory(r, b.HostXpra.target, 3, 1)
		},
		"generation": func(m *Manager, _ *hostWindowFakeRunner, b Binding, _ NativeFrame) {
			b.RunID = "replacement-run"
			m.bindings[b.Surface.SurfaceID] = b
		},
		"preflight-rebind": func(m *Manager, r *hostWindowFakeRunner, b Binding, _ NativeFrame) {
			r.onList = func() {
				m.mu.Lock()
				defer m.mu.Unlock()
				b.RunID = "replacement-run"
				m.bindings[b.Surface.SurfaceID] = b
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			m, r, b := ownedHostWindowFixture(t)
			_, f, err := m.Capture(context.Background(), b.Surface.SurfaceID)
			if err != nil {
				t.Fatal(err)
			}
			mutate(m, r, b, f)
			if err := hostTestInput(m, b, "altered"); err == nil || r.count("input") != 0 {
				t.Fatalf("changed frame reached input: %v %v", err, r.calls)
			}
			t.Log("input=0 after observation invalidation")
		})
	}
}

func TestOwnedNativeInputKeepsReceiptValidation(t *testing.T) {
	m, r, b := ownedHostWindowFixture(t)
	id := b.Surface.SurfaceID
	runInput := func(proposal string, action map[string]any) error {
		if _, _, err := m.Capture(context.Background(), id); err != nil {
			t.Fatal(err)
		}
		params, _ := jsonParams(id, proposal, "agent", action)
		_, err := m.Handle(context.Background(), req("input", params))
		return err
	}
	click := map[string]any{"type": "click", "x": 1, "y": 0, "button": "left"}
	if err := runInput("click", click); err != nil {
		t.Fatal(err)
	}
	if err := runInput("type", map[string]any{"type": "type", "text": "fixture text"}); err != nil {
		t.Fatal(err)
	}
	const ack = `{"ok":true,"xid":77594627,"witness":"fixture-window","kind":"click","focus_verified":true}`
	for i, altered := range []string{
		`{}`, strings.Replace(ack, "77594627", "77594628", 1), strings.Replace(ack, "fixture-window", "foreign-window", 1),
		strings.Replace(ack, `"focus_verified":true`, `"focus_verified":false`, 1),
		strings.Replace(ack, `"kind":"click"`, `"kind":"key"`, 1),
		strings.TrimSuffix(ack, "}") + `,"extra":1}`,
	} {
		r.inputOut = []byte(altered)
		if err := runInput(fmt.Sprintf("invalid-ack-%d", i), click); err == nil {
			t.Fatalf("invalid receipt accepted: %s", altered)
		}
	}
}

func TestOwnedNativeCaptureReconcilesResizeAndMinimize(t *testing.T) {
	m, r, b := ownedHostWindowFixture(t)
	id := b.Surface.SurfaceID
	setHostInventory(r, b.HostXpra.target, 4, 2)
	r.captureOut = makeTestXWD(4, 2, 42)
	current, f, err := m.Capture(context.Background(), id)
	if err != nil || current.Surface.Geometry != (Geometry{W: 4, H: 2}) || f.Width != 4 || f.Height != 2 {
		t.Fatalf("resize capture did not preserve geometry: %+v %+v %v", current, f, err)
	}
	r.listOut = bytes.Replace(r.listOut, []byte(`"visible":true`), []byte(`"visible":false,"minimized":true`), 1)
	current, _, err = m.Capture(context.Background(), id)
	if !IsHostWindowWaiting(err) || !current.HostMinimized || current.Surface.State != "active" || r.count("capture") != 1 {
		t.Fatalf("minimized window was captured or detached: %+v %v", current, err)
	}
}

func TestUnownedStagedDesktopWindowRefusedOnCreate(t *testing.T) {
	m, r, b := ownedHostWindowFixture(t)
	profile := "hw-unowned"
	r.windows = map[string]*HostWindowSpec{
		profile: {XID: b.HostXID, Witness: b.HostWitness, Display: b.HostDisplay, Xauthority: b.HostXauthority},
	}
	params, _ := json.Marshal(map[string]any{"kind": "desktop_capsule", "profile_id": profile, "geometry": Geometry{W: 2, H: 1}})
	if _, err := m.Handle(context.Background(), req("create", string(params))); err == nil || len(r.calls) != 0 {
		t.Fatalf("staged opening reached native code without process custody: %v %v", err, r.calls)
	}
}

func TestH1NativePublishedFrameRecheckBeforeDispatch(t *testing.T) {
	m, runner, binding := ownedHostWindowFixture(t)
	_, frame, err := m.Capture(context.Background(), binding.Surface.SurfaceID)
	if err != nil {
		t.Fatal(err)
	}
	witness := frame.PublishedHostFrame(frame.Path)
	current := true
	runner.onList = func() { current = false }
	params, _ := jsonParams(binding.Surface.SurfaceID, "replaced-publication", "agent",
		map[string]any{"type": "click", "x": 0, "y": 0, "button": "left"})
	before := len(runner.calls)
	_, _, err = m.HandleInputWithPublishedFrame(context.Background(), req("input", params), func() (PublishedHostFrame, bool) {
		return witness, current
	})
	if err == nil || current || len(runner.calls) != before+1 || runner.calls[before][1] != "list" {
		t.Fatalf("publication replacement during inventory was not fenced: %v %v", err, runner.calls[before:])
	}
}
