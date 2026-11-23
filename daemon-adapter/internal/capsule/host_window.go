package capsule

// [TARGET, unit decision] Host program keys request an 80 ms native press/release gap.
// Requires the private srf-host-window key-hold option; unsupported binaries
// refuse the request. Browser input and contract messages are unchanged.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"unicode"
	"unicode/utf8"

	"metaviewer/daemon-adapter/internal/cli"
)

// [TARGET, unit decision] Resize is capture progress, not absence. Yield one
// attempt to the paced subscription; never retry in an unbounded capture loop.
var hostWindowResizePattern = regexp.MustCompile(`^(?:host_window_unavailable:\s*)*resized: [0-9]+x[0-9]+ -> ([0-9]+)x([0-9]+)$`)

// HostWindowResizeGeometry accepts only the native bounded resize diagnostic.
func HostWindowResizeGeometry(reason string) (int, int, bool) {
	match := hostWindowResizePattern.FindStringSubmatch(strings.TrimSpace(reason))
	if match == nil {
		return 0, 0, false
	}
	w, ew := strconv.Atoi(match[1])
	h, eh := strconv.Atoi(match[2])
	return w, h, ew == nil && eh == nil && w >= 1 && h >= 1 && w <= 4096 && h <= 4096
}

// IsHostWindowResize identifies a validated geometry change from capture.
func IsHostWindowResize(err error) bool {
	var f *cli.Fault
	return errors.As(err, &f) && f.Code == "host_window_resized"
}

// IsHostWindowWaiting distinguishes a witnessed minimize gap from terminal loss.
func IsHostWindowWaiting(err error) bool {
	var f *cli.Fault
	return errors.As(err, &f) && f.Code == "host_window_unavailable" && isHostWindowMinimizedReason(f.Detail)
}

func isHostWindowMinimizedReason(s string) bool {
	lower := strings.ToLower(s)
	return strings.Contains(lower, "minimized") || strings.Contains(lower, "iconic") || strings.Contains(lower, "unmapped")
}

func isHostWindowTerminalReason(s string) bool {
	lower := strings.ToLower(s)
	if isHostWindowMinimizedReason(lower) {
		return false
	}
	return strings.Contains(lower, "host_window_unavailable:") ||
		strings.Contains(lower, "witness") ||
		strings.Contains(lower, "closed") ||
		strings.Contains(lower, "no such window") ||
		strings.Contains(lower, "badwindow") ||
		strings.Contains(lower, "baddrawable") ||
		strings.Contains(lower, "destroyed") ||
		strings.Contains(lower, "not found") ||
		strings.Contains(lower, "gone")
}

// DecodeHostWindowXWD decodes one big-endian 32-bpp ZPixmap XWD frame
// (srf-fbread getimage / srf-host-window capture format) into RGB24 bytes.
func DecodeHostWindowXWD(data []byte) ([]byte, int, int, error) {
	if len(data) < 100 {
		return nil, 0, 0, errors.New("XWD frame too short")
	}
	be := binary.BigEndian
	size := int(be.Uint32(data[0:]))
	w, ht := int(be.Uint32(data[16:])), int(be.Uint32(data[20:]))
	order, bpp, bpl, ncolors := be.Uint32(data[28:]), be.Uint32(data[44:]), int(be.Uint32(data[48:])), int(be.Uint32(data[76:]))
	if size < 100 || size > 4096 || be.Uint32(data[4:]) != 7 || be.Uint32(data[8:]) != 2 || order > 1 || bpp != 32 || w < 1 || ht < 1 || w > 4096 || ht > 4096 || bpl < 4*w || bpl > 4*w+64 || ncolors < 0 || ncolors > 256 {
		return nil, 0, 0, errors.New("invalid XWD header")
	}
	off := size + ncolors*12
	need := off + bpl*ht
	if len(data) != need {
		return nil, 0, 0, errors.New("invalid XWD byte extent")
	}
	pix := data[off:need]
	rgb := make([]byte, w*ht*3)
	for y := 0; y < ht; y++ {
		src := pix[y*bpl:]
		dst := rgb[y*w*3:]
		for x := 0; x < w; x++ {
			if order == 0 {
				dst[x*3], dst[x*3+1], dst[x*3+2] = src[x*4+2], src[x*4+1], src[x*4]
			} else {
				dst[x*3], dst[x*3+1], dst[x*3+2] = src[x*4+1], src[x*4+2], src[x*4+3]
			}
		}
	}
	return rgb, w, ht, nil
}

type hostWindowObservation struct {
	XID       uint32 `json:"xid"`
	Witness   string `json:"witness"`
	Title     string `json:"title"`
	PID       int    `json:"pid"`
	Type      string `json:"window_type"`
	Visible   bool   `json:"visible"`
	Minimized bool   `json:"minimized"`
	Rect      struct {
		W int `json:"width"`
		H int `json:"height"`
	} `json:"rect"`
}

func (w hostWindowObservation) target(p *hostXpraProcess) hostWindowTarget {
	if w.XID == 0 || !validHostWitness(w.Witness) || w.PID == os.Getpid() ||
		w.Type != "normal" || !w.Visible || w.Minimized {
		return hostWindowTarget{}
	}
	return hostWindowTarget{w.XID, w.Witness, w.PID, p.processStart(w.PID)}
}

func (t hostWindowTarget) current(p *hostXpraProcess) bool {
	return t.start != "" && t.pid != os.Getpid() && p.processStart(t.pid) == t.start
}

func (m *Manager) listOwnedHostWindows(ctx context.Context, p *hostXpraProcess) ([]hostWindowObservation, error) {
	out, err := m.executeRawHostWindow(ctx, p, []string{"list", "--exclude-pid", strconv.Itoa(os.Getpid())})
	if err != nil {
		return nil, err
	}
	var reply struct {
		Windows []hostWindowObservation `json:"windows"`
	}
	if err := json.Unmarshal(out, &reply); err != nil || reply.Windows == nil {
		return nil, fault("host_window_unavailable", "invalid native window inventory")
	}
	return reply.Windows, nil
}

func (m *Manager) observeBoundWindow(ctx context.Context, b Binding) (Geometry, error) {
	rows, err := m.listOwnedHostWindows(ctx, b.HostXpra)
	if err != nil {
		return Geometry{}, err
	}
	var geometry Geometry
	for _, row := range rows {
		if row.XID != b.HostXID {
			continue
		}
		target := b.HostXpra.target
		if geometry != (Geometry{}) || row.Witness != target.witness || row.PID != target.pid ||
			!target.current(b.HostXpra) || row.Type != "normal" {
			return Geometry{}, fault("host_window_unavailable", "host_window_unavailable: window witness changed")
		}
		if !row.Visible || row.Minimized {
			return Geometry{}, fault("host_window_unavailable", "host_window_unavailable: window minimized")
		}
		if row.Rect.W < 1 || row.Rect.H < 1 || row.Rect.W > 4096 || row.Rect.H > 4096 {
			return Geometry{}, fault("host_window_unavailable", "bound window geometry unavailable")
		}
		geometry = Geometry{W: row.Rect.W, H: row.Rect.H}
	}
	if geometry == (Geometry{}) {
		return Geometry{}, fault("host_window_unavailable", "bound window absent from private inventory")
	}
	return geometry, nil
}

func (b Binding) ownsHostWindow() bool {
	return b.HostDisplayCapture().Valid()
}

// HostDisplayCapture carries the launcher's display and window authority into
// the independent DISPLAY tap. Its fields can only be set from a binding.
type HostDisplayCapture struct {
	owner                               *hostXpraProcess
	binDir, display, authority, witness string
	xid                                 uint32
}

func (b Binding) HostDisplayCapture() HostDisplayCapture {
	if b.NativeKind != "host-xpra" || b.Surface.Display != b.HostDisplay {
		return HostDisplayCapture{}
	}
	return HostDisplayCapture{b.HostXpra, b.BinDir, b.HostDisplay, b.HostXauthority, b.HostWitness, b.HostXID}
}

func (c HostDisplayCapture) Valid() bool {
	p := c.owner
	return p.displayOwned(c.display, c.authority) && p.target.xid == c.xid &&
		p.target.witness == c.witness && p.target.current(p)
}

func (c HostDisplayCapture) Command(ctx context.Context) *exec.Cmd {
	if !c.Valid() || !filepath.IsAbs(c.binDir) {
		return nil
	}
	args := []string{"capture", "--xid", strconv.FormatUint(uint64(c.xid), 10), "--witness", c.witness}
	cmd := exec.CommandContext(ctx, filepath.Join(c.binDir, "srf-host-window"), append(args, HostWindowSessionArgs(c.display, c.authority)...)...)
	cmd.Env = c.owner.environment(c.owner.searchPath)
	return cmd
}

func sameHostWindowBinding(a, b Binding) bool {
	return a.Surface.SurfaceID == b.Surface.SurfaceID && a.NativeKind == b.NativeKind &&
		a.Container == b.Container && a.RunID == b.RunID && a.Window == b.Window &&
		a.Surface.Display == b.Surface.Display && a.HostDisplay == b.HostDisplay &&
		a.HostXauthority == b.HostXauthority && a.HostXID == b.HostXID &&
		a.HostWitness == b.HostWitness && a.HostXpra == b.HostXpra &&
		a.FramesRoot == b.FramesRoot
}

// A staged desktop pick has no launcher-owned process or display witness.
func (m *Manager) attachHostWindow(_, _, _ string, _ Profile, _ *HostWindowSpec, _ Geometry) ([]json.RawMessage, error) {
	return nil, fault("host_window_unavailable", "owned private display required for native attach")
}

func (m *Manager) attachOwnedHostWindow(qProfileID, id, version string, p Profile, spec *HostWindowSpec, geom Geometry, process *hostXpraProcess) ([]json.RawMessage, error) {
	if err := ValidateHostWindowSpec(spec); err != nil {
		return nil, err
	}
	if p.Display != spec.Display || !process.displayOwned(spec.Display, spec.Xauthority) ||
		process.target.xid != spec.XID || process.target.witness != spec.Witness || !process.target.current(process) {
		return nil, fault("host_window_unavailable", "owned private display and process required for native attach")
	}
	container := "srf-pg-hx-" + qProfileID
	shortID := strings.TrimPrefix(id, "surface-")
	if len(shortID) > 16 {
		shortID = shortID[:16]
	}
	runIDStr := "run-hw-" + shortID
	framesRoot := filepath.Join(p.RAMRoot, runIDStr, "frames", "w1")
	for _, old := range m.bindings {
		if old.Surface.State != "destroyed" && (old.RunID == runIDStr || old.FramesRoot == framesRoot || under(old.FramesRoot, framesRoot) || under(framesRoot, old.FramesRoot) || old.Container == container) {
			return nil, fault("binding", "native run or frame allocation already observed")
		}
	}
	if err := os.MkdirAll(framesRoot, 0700); err != nil {
		return nil, fault("configuration", "failed to allocate host-window frames directory")
	}
	if err := os.Chmod(framesRoot, 0700); err != nil {
		return nil, fault("configuration", "failed to secure host-window frames directory")
	}
	b := Binding{
		Role: "agent",
		Surface: Surface{
			Schema:     "surface/" + version,
			SurfaceID:  id,
			Kind:       "desktop_capsule",
			Display:    p.Display,
			Geometry:   geom,
			State:      "active",
			Visibility: "unfolded",
			Control:    "agent",
			ProfileID:  qProfileID,
		},
		Container:      container,
		RunID:          runIDStr,
		Window:         "w1",
		FramesRoot:     framesRoot,
		NativeKind:     "host-xpra",
		HostXpra:       process,
		Program:        spec.Program,
		HostXID:        spec.XID,
		HostWitness:    spec.Witness,
		HostTitle:      spec.Title,
		HostProgram:    spec.Program,
		HostDisplay:    spec.Display,
		HostXauthority: spec.Xauthority,
		BinDir:         m.cfg.BinDir,
	}
	m.bindings[id] = b
	delete(m.quarantined, qProfileID)
	return []json.RawMessage{surfaceEvent(version, "created", b.Surface)}, nil
}

func (m *Manager) detachHostWindowLocked(b Binding) error {
	delete(m.hostSeq, b.Surface.SurfaceID)
	if profile, ok := m.cfg.Profiles[b.Surface.ProfileID]; ok && profile.RAMRoot != "" && b.RunID != "" {
		runDir := filepath.Join(profile.RAMRoot, b.RunID)
		if under(profile.RAMRoot, runDir) {
			_ = os.RemoveAll(runDir)
		}
	}
	return nil
}

func writeHostWindowFrameFile(framesRoot string, seq uint64, rgb []byte) (string, error) {
	if err := os.MkdirAll(framesRoot, 0700); err != nil {
		return "", err
	}
	entries, _ := os.ReadDir(framesRoot)
	name := fmt.Sprintf("frame-%d.rgb24", seq)
	tmpPath := filepath.Join(framesRoot, name+".tmp")
	finalPath := filepath.Join(framesRoot, name)
	if err := os.WriteFile(tmpPath, rgb, 0600); err != nil {
		return "", err
	}
	if err := os.Rename(tmpPath, finalPath); err != nil {
		_ = os.Remove(tmpPath)
		return "", err
	}
	for _, entry := range entries {
		if entry.Name() != name && strings.HasPrefix(entry.Name(), "frame-") {
			_ = os.Remove(filepath.Join(framesRoot, entry.Name()))
		}
	}
	return finalPath, nil
}

// [TARGET, unit decision] A capture/input can fence the window before the next
// subscription poll. Preserve its pending loss through every capture entry so
// the wire owner can deliver state_changed before capture_failed, without IO.
// Caller holds m.mu; this is evidence for delivery, never capture admission.
func (m *Manager) hasPendingHostLossLocked(b Binding) bool {
	return (b.NativeKind == "host-window" || b.NativeKind == "host-xpra") &&
		b.Surface.State == "lost" && b.ConfirmedAbsent &&
		len(b.pendingHostLost) > 0 && !m.destroying[b.Surface.SurfaceID]
}

func (m *Manager) captureHostWindow(ctx context.Context, id string) (Binding, NativeFrame, error) {
	m.mu.Lock()
	b, ok := m.bindings[id]
	if ok && m.hasPendingHostLossLocked(b) {
		m.mu.Unlock()
		return b, NativeFrame{}, fault("host_window_unavailable", b.HostLostReason)
	}
	if !ok || b.Surface.State != "active" || b.recoveryProbe != nil || m.destroying[id] {
		m.mu.Unlock()
		return Binding{}, NativeFrame{}, fault("surface", "capture requires an active surface")
	}
	if m.hasPending(id) {
		m.mu.Unlock()
		return Binding{}, NativeFrame{}, fault("POLICY", "input must be drained before capture")
	}
	if !b.ownsHostWindow() {
		m.mu.Unlock()
		return b, NativeFrame{}, fault("host_window_unavailable", "owned private window required before capture")
	}
	b.HostXpra.frame = hostFrameWitness{}
	epoch := m.beginCapture(id)
	args := []string{"capture", "--xid", strconv.FormatUint(uint64(b.HostXID), 10), "--witness", b.HostWitness, "--max-frames", "1"}
	before := m.clock()
	m.mu.Unlock()

	var out []byte
	observed, execErr := m.observeBoundWindow(ctx, b)
	if execErr == nil {
		if !b.ownsHostWindow() {
			execErr = fault("host_window_unavailable", "owned private window changed before capture")
		} else {
			out, execErr = m.executeRawHostWindow(ctx, b.HostXpra, args)
		}
	}
	after := m.clock()

	var rgb []byte
	var w, h int
	var decodeErr error
	if execErr == nil {
		rgb, w, h, decodeErr = DecodeHostWindowXWD(out)
		if decodeErr != nil {
			if reason := extractHostWindowRefusal(string(out)); reason != "" {
				execErr = fault("host_window_unavailable", reason)
			} else {
				execErr = fault("frame", "invalid host window XWD frame")
			}
		}
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	current, ok := m.bindings[id]
	if !ok || current.Surface.State != "active" || current.recoveryProbe != nil || m.destroying[id] || !sameHostWindowBinding(current, b) {
		return Binding{}, NativeFrame{}, fault("surface", "capture requires an active surface")
	}
	if m.hasPending(id) || m.inputEpochs[id] != epoch || current.FrameRefreshEpoch != b.FrameRefreshEpoch || current.Surface.Geometry != b.Surface.Geometry {
		return Binding{}, NativeFrame{}, fault("POLICY", "input must be drained before capture")
	}
	if execErr != nil {
		errText := execErr.Error()
		if w, h, resized := HostWindowResizeGeometry(errText); resized {
			m.updateHostWindowGeometryLocked(id, w, h)
			return m.bindings[id], NativeFrame{}, fault("host_window_resized", "capture geometry changed")
		}
		if isHostWindowMinimizedReason(errText) {
			if !current.HostMinimized {
				current.FrameRefreshEpoch++
			}
			current.HostMinimized = true
			m.bindings[id] = current
			return current, NativeFrame{}, fault("host_window_unavailable", extractOrDefaultHostWindowReason(errText, "host_window_unavailable: window minimized"))
		}
		if isHostWindowTerminalReason(errText) {
			reason := extractOrDefaultHostWindowReason(errText, "host_window_unavailable: window closed or witness changed")
			current.HostLostReason = reason
			m.bindings[id] = current
			ev, _ := m.markLostLocked(id, true, false)
			lost := m.bindings[id]
			lost.pendingHostLost = ev
			m.bindings[id] = lost
			return m.bindings[id], NativeFrame{}, fault("host_window_unavailable", reason)
		}
		return current, NativeFrame{}, execErr
	}
	if !current.ownsHostWindow() || observed != (Geometry{W: w, H: h}) {
		return current, NativeFrame{}, fault("frame", "native capture window or geometry changed")
	}
	m.updateHostWindowGeometryLocked(id, w, h)
	current = m.bindings[id]
	if m.hostSeq == nil {
		m.hostSeq = make(map[string]uint64)
	}
	m.hostSeq[id]++
	seq := m.hostSeq[id]
	framePath, writeErr := writeHostWindowFrameFile(current.FramesRoot, seq, rgb)
	if writeErr != nil || !under(current.FramesRoot, framePath) {
		return current, NativeFrame{}, fault("frame", "native frame allocation mismatch")
	}
	m.bindings[id] = current
	delete(m.refresh, id)
	sum := sha256.Sum256(rgb)
	mono := after
	if mono < before {
		mono = before
	}
	if mono < m.cfg.OriginNS {
		mono = m.cfg.OriginNS
	}
	f := NativeFrame{
		Path:          framePath,
		Seq:           seq,
		SHA256RGB24:   hex.EncodeToString(sum[:]),
		Width:         w,
		Height:        h,
		CaptureMonoNS: mono,
		Generation:    current.Container,

		Window: &Window{
			ID:    current.Window,
			W:     w,
			H:     h,
			Title: current.HostTitle,
		},
		TSNS:      mono - m.cfg.OriginNS,
		CaptureUS: (after - before) / 1000,
	}
	f.hostSource = &hostFrameSource{
		owner: current.HostXpra, surfaceID: id, runID: current.RunID,
		refreshEpoch: current.FrameRefreshEpoch, inputEpoch: epoch,
		tick: f.Seq, generation: f.Generation, path: f.Path, digest: f.SHA256RGB24,
		geometry: Geometry{W: w, H: h},
	}
	current.HostXpra.frame = hostFrameWitness{
		Frame: f, SurfaceID: id, RunID: current.RunID,
		RefreshEpoch: current.FrameRefreshEpoch, InputEpoch: epoch,
	}
	return current, f, nil
}

// Keep the latest direct observation alongside the launcher-owned window
// witness. The wire input contract supplies no native frame filename.
type hostFrameWitness struct {
	Frame                    NativeFrame
	SurfaceID, RunID         string
	RefreshEpoch, InputEpoch uint64
}

func (f hostFrameWitness) validFor(b Binding, inputEpoch uint64) bool {
	frame := f.Frame
	g := b.Surface.Geometry
	if frame.Seq == 0 || f.SurfaceID != b.Surface.SurfaceID || f.RunID != b.RunID ||
		f.RefreshEpoch != b.FrameRefreshEpoch || f.InputEpoch != inputEpoch ||
		frame.Generation != b.Container || b.HostMinimized || frame.Width != g.W || frame.Height != g.H ||
		g.W < 1 || g.H < 1 || g.W > 4096 || g.H > 4096 ||
		filepath.Dir(frame.Path) != b.FramesRoot {
		return false
	}
	return validHostFrameBytes(frame.Path, frame.SHA256RGB24, g)
}

// hostFrameSource binds a verified native capture to its launcher and epochs.
type hostFrameSource struct {
	owner                          *hostXpraProcess
	surfaceID, runID               string
	refreshEpoch, inputEpoch, tick uint64
	generation, path, digest       string
	geometry                       Geometry
}

// PublishedHostFrame carries the source identity and the publisher's RGB24
// allocation. It is derived from the exact native frame that was published.
type PublishedHostFrame struct {
	source *hostFrameSource
	path   string
}

func (f NativeFrame) PublishedHostFrame(path string) PublishedHostFrame {
	s := f.hostSource
	if s == nil || s.path != f.Path || s.tick != f.Seq || s.generation != f.Generation ||
		s.digest != f.SHA256RGB24 || s.geometry != (Geometry{W: f.Width, H: f.Height}) {
		return PublishedHostFrame{}
	}
	return PublishedHostFrame{source: s, path: path}
}

func (f PublishedHostFrame) validFor(b Binding, inputEpoch uint64) bool {
	source := f.source
	g := b.Surface.Geometry
	return source != nil && source.owner == b.HostXpra && source.surfaceID == b.Surface.SurfaceID &&
		source.runID == b.RunID && source.refreshEpoch == b.FrameRefreshEpoch &&
		source.inputEpoch == inputEpoch && source.generation == b.Container &&
		source.geometry == g && !b.HostMinimized &&
		validHostFrameBytes(f.path, source.digest, g)
}

func validHostFrameBytes(path, digest string, g Geometry) bool {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return false
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	size := int64(g.W) * int64(g.H) * 3
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() != size {
		return false
	}
	hash := sha256.New()
	n, err := io.Copy(hash, io.LimitReader(file, size+1))
	if err != nil || n != size || hex.EncodeToString(hash.Sum(nil)) != digest {
		return false
	}
	current, err := os.Lstat(path)
	return err == nil && os.SameFile(info, current) && current.Size() == size && current.ModTime() == info.ModTime()
}

func extractOrDefaultHostWindowReason(raw, fallback string) string {
	if r := extractHostWindowRefusal(raw); r != "" {
		return r
	}
	return fallback
}

func (m *Manager) executeRawHostWindow(ctx context.Context, p *hostXpraProcess, args []string) ([]byte, error) {
	if p == nil || !p.displayOwned(p.display, p.authority) {
		return nil, fault("host_window_unavailable", "owned private display required before native access")
	}
	ctx, cancel := context.WithTimeout(ctx, m.cfg.Timeout)
	defer cancel()
	argv := append([]string{filepath.Join(m.cfg.BinDir, "srf-host-window")}, args...)
	argv = append(argv, HostWindowSessionArgs(p.display, p.authority)...)
	if r, ok := m.runner.(*ProcessRunner); ok {
		private := *r
		private.env = p.environment(r.SearchPath())
		return private.Run(ctx, argv, nil)
	}
	return m.runner.Run(ctx, argv, nil)
}

func (m *Manager) inputHostWindow(ctx context.Context, surfaceID, proposalID, origin, actionType string, x, y *int, button string, keys []string, text string, version string, evidence *InputEvidence, published func() (PublishedHostFrame, bool)) ([]json.RawMessage, error) {
	b := m.bindings[surfaceID]
	if !b.ownsHostWindow() {
		return nil, fault("host_window_unavailable", "owned private window required before input")
	}
	frame := b.HostXpra.frame
	frameEpoch := m.inputEpochs[surfaceID]
	frameValid := func(current Binding) bool {
		if published == nil {
			return frame.validFor(current, frameEpoch)
		}
		witness, ok := published()
		if !ok || !witness.validFor(current, frameEpoch) {
			return false
		}
		latest, ok := published()
		return ok && latest == witness
	}
	if !frameValid(b) {
		return nil, fault("frame", "current native frame witness required before input")
	}
	xidStr := strconv.FormatUint(uint64(b.HostXID), 10)
	baseArgs := []string{"input", "--xid", xidStr, "--witness", b.HostWitness}
	var callArgvs [][]string
	var textChunks [][]byte
	nativeAction := actionType

	switch actionType {
	case "click":
		_, valid := map[string]string{"left": "1", "middle": "2", "right": "3"}[button]
		if !valid || x == nil || y == nil || *x < 0 || *y < 0 || *x >= b.Surface.Geometry.W || *y >= b.Surface.Geometry.H || len(keys) != 0 || text != "" {
			return nil, fault("input", "invalid click")
		}
		callArgvs = [][]string{append(append([]string(nil), baseArgs...), "--click", strconv.Itoa(*x), strconv.Itoa(*y))}
	case "key":
		if len(keys) < 1 || len(keys) > 8 || x != nil || y != nil || button != "" || text != "" {
			return nil, fault("input", "invalid key")
		}
		key := strings.Join(keys, "+")
		if !regexp.MustCompile(`^[A-Za-z0-9_+]{1,32}$`).MatchString(key) {
			return nil, fault("input", "invalid key chord")
		}
		keyArgs := append(append([]string(nil), baseArgs...), "--key", key)
		if b.NativeKind == "host-xpra" {
			keyArgs = append(keyArgs, "--key-hold-ms", "80")
		}
		callArgvs = [][]string{keyArgs}
	case "type":
		if len(text) == 0 || x != nil || y != nil || button != "" || len(keys) != 0 {
			return nil, fault("input", "invalid text")
		}
		if utf8.RuneCountInString(text) > AdapterTextMaxRunes {
			return nil, fault("TEXT_TOO_LONG", "text exceeds adapter rune limit")
		}
		for _, r := range text {
			if !unicode.IsPrint(r) {
				return nil, fault("input", "text must contain printable runes")
			}
		}
		runes := []rune(text)
		for len(runes) > 0 {
			n := min(len(runes), NativeTextMaxRunes)
			chunk := string(runes[:n])
			textChunks = append(textChunks, []byte(chunk))
			callArgvs = append(callArgvs, append(append([]string(nil), baseArgs...), "--type", chunk))
			runes = runes[n:]
		}
		nativeAction = "text"
	default:
		return nil, cli.Unsupported("input action")
	}

	run, cancel := context.WithCancel(ctx)
	pending := &pendingInput{surface: surfaceID, epoch: m.nextInputEpoch(surfaceID), cancel: cancel}
	m.pending[proposalID] = pending
	m.used[proposalID] = true
	defer func() {
		cancel()
		pending.done = true
		if current, ok := m.bindings[surfaceID]; ok && sameHostWindowBinding(current, b) {
			// Deduplication must publish a fresh witness even when input leaves
			// the pixels unchanged.
			current.FrameRefreshEpoch++
			m.bindings[surfaceID] = current
		}
		if !pending.cancelled {
			delete(m.pending, proposalID)
		}
	}()

	dispatched := b
	var receipt NativeInputReceipt
	var sequence *TextSequenceEvidence
	if len(textChunks) > 1 {
		sequence = &TextSequenceEvidence{}
	}
	for i, argv := range callArgvs {
		if err := run.Err(); err != nil {
			return nil, err
		}
		m.mu.Unlock()
		observed, err := m.observeBoundWindow(run, dispatched)
		m.mu.Lock()
		current, ok := m.bindings[surfaceID]
		if pending.cancelled {
			return nil, fault("SURFACE_NOT_AGENT_CONTROLLED", "input cancelled by control takeover")
		}
		if run.Err() != nil || !ok || !sameHostWindowBinding(current, dispatched) ||
			current.Surface.State != "active" || current.Surface.Control != origin ||
			current.recoveryProbe != nil || m.destroying[surfaceID] || m.inputEpochs[surfaceID] != pending.epoch ||
			!current.ownsHostWindow() || !frameValid(current) {
			return nil, fault("POLICY", "native window or frame changed before input")
		}
		if err != nil {
			return nil, err
		}
		if observed != current.Surface.Geometry {
			m.updateHostWindowGeometryLocked(surfaceID, observed.W, observed.H)
			return nil, fault("frame", "native geometry changed before input")
		}
		if os.Getenv("MV_INPUT_DIAGNOSTICS") == "1" {
			fmt.Fprintf(os.Stderr, "METAVIEWER_HOST_INPUT stage=dispatch action=%s\n", actionType)
		}
		before := m.clock()
		m.mu.Unlock()
		out, execErr := m.executeRawHostWindow(run, current.HostXpra, argv)
		after := m.clock()
		m.mu.Lock()
		if pending.cancelled {
			return nil, fault("SURFACE_NOT_AGENT_CONTROLLED", "input cancelled by control takeover")
		}
		b, ok := m.bindings[surfaceID]
		if !ok || b.Surface.State != "active" || b.recoveryProbe != nil || m.destroying[surfaceID] || !sameHostWindowBinding(b, dispatched) || !b.ownsHostWindow() {
			return nil, fault("surface", "surface lost during input")
		}
		if b.Surface.Control != origin {
			if origin == "human" {
				return nil, fault("SURFACE_NOT_HUMAN_CONTROLLED", "agent control blocks human input")
			}
			return nil, fault("SURFACE_NOT_AGENT_CONTROLLED", "human control blocks agent input")
		}
		if execErr == nil {
			if reason := extractHostWindowRefusal(string(out)); reason != "" {
				execErr = fault("host_window_unavailable", reason)
			}
		}
		if execErr != nil {
			errText := execErr.Error()
			if isHostWindowTerminalReason(errText) {
				reason := extractOrDefaultHostWindowReason(errText, "host_window_unavailable: window closed or witness changed")
				b.HostLostReason = reason
				m.bindings[surfaceID] = b
				ev, _ := m.markLostLocked(surfaceID, true, false)
				lost := m.bindings[surfaceID]
				lost.pendingHostLost = ev
				m.bindings[surfaceID] = lost
				return nil, fault("host_window_unavailable", reason)
			}
			return nil, execErr
		}
		receipt = NativeInputReceipt{}
		trimmed := bytes.TrimSpace(out)
		if b.NativeKind == "host-xpra" || b.NativeKind == "host-window" {
			var ack struct {
				OK            *bool  `json:"ok"`
				XID           uint32 `json:"xid"`
				Witness       string `json:"witness"`
				Kind          string `json:"kind"`
				FocusVerified *bool  `json:"focus_verified"`
			}
			if e := cli.ParseLine(out, m.cfg.MaxOutputBytes, &ack); e != nil {
				return nil, e
			}
			expected := nativeAction
			if expected == "text" {
				expected = "type"
			}
			if ack.OK == nil || !*ack.OK || ack.XID != b.HostXID || ack.Witness != b.HostWitness || ack.Kind != expected || ack.FocusVerified == nil || !*ack.FocusVerified {
				return nil, fault("input", "unverified host-window input receipt")
			}
			if os.Getenv("MV_INPUT_DIAGNOSTICS") == "1" {
				fmt.Fprintln(os.Stderr, "METAVIEWER_HOST_INPUT stage=ack verified=true")
			}
			receipt.Kind = nativeAction
			receipt.FocusVerified = ack.FocusVerified
		} else if len(trimmed) > 0 && trimmed[0] == '{' {
			if e := cli.ParseLine(out, m.cfg.MaxOutputBytes, &receipt); e != nil {
				return nil, e
			}
			if receipt.Result == "refused" {
				return nil, fault("refused", "native input refused")
			}
		}
		mono := after
		if mono < before {
			mono = before
		}
		if mono < m.cfg.OriginNS {
			mono = m.cfg.OriginNS
		}
		if receipt.InputMonoNS != nil {
			if *receipt.InputMonoNS < m.cfg.OriginNS || *receipt.InputMonoNS > 9007199254740991 || *receipt.InputMonoNS < before || *receipt.InputMonoNS > after || after < before {
				return nil, fault("input", "unverified input receipt")
			}
		} else {
			receipt.InputMonoNS = &mono
		}
		if receipt.SurfaceID == "" {
			receipt.SurfaceID = b.Window
		}
		if receipt.Kind == "" {
			receipt.Kind = nativeAction
		}
		if receipt.Result == "" {
			receipt.Result = "ok"
		}
		if receipt.FocusVerified == nil {
			fv := true
			receipt.FocusVerified = &fv
		}
		if receipt.SurfaceID != b.Window || receipt.Kind != nativeAction || !*receipt.FocusVerified || receipt.Result != "ok" {
			return nil, fault("input", "unverified input receipt")
		}
		if sequence != nil && i < len(textChunks) {
			sum := sha256.Sum256(textChunks[i])
			sequence.Chunks = append(sequence.Chunks, TextChunkEvidence{NativeInputReceipt: receipt, Runes: utf8.RuneCount(textChunks[i]), SHA256: hex.EncodeToString(sum[:])})
		}
	}
	if m.bindings[surfaceID].Surface.State != "active" {
		return nil, fault("surface", "surface lost during input verification")
	}
	if evidence != nil {
		*evidence = InputEvidence{
			TextSequence:    sequence,
			SurfaceID:       surfaceID,
			NativeSurfaceID: receipt.SurfaceID,
			Kind:            receipt.Kind,
			InputMonoNS:     *receipt.InputMonoNS,
			InputTSNS:       *receipt.InputMonoNS - m.cfg.OriginNS,
			FocusVerified:   receipt.FocusVerified,
			Result:          receipt.Result,
		}
	}
	return []json.RawMessage{event(version, "input_applied", struct {
		SurfaceID  string `json:"surface_id"`
		ProposalID string `json:"proposal_id"`
	}{surfaceID, proposalID})}, nil
}

// UpdateHostWindowGeometry updates the bound surface and profile dimensions
// when the resident capture tap observes a window resize.
func (m *Manager) UpdateHostWindowGeometry(id string, w, h int) bool {
	if w < 1 || h < 1 || w > 4096 || h > 4096 {
		return false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.updateHostWindowGeometryLocked(id, w, h)
}

func (m *Manager) updateHostWindowGeometryLocked(id string, w, h int) bool {
	b, ok := m.bindings[id]
	if !ok || b.Surface.State != "active" || m.destroying[id] ||
		!((b.NativeKind == "host-window" || b.NativeKind == "host-xpra") ||
			((b.NativeKind == "browser" || b.NativeKind == "program") && b.Window != "")) {
		return false
	}
	changed := b.Surface.Geometry.W != w || b.Surface.Geometry.H != h
	if changed && b.HostXpra != nil {
		b.FrameRefreshEpoch++
		b.HostXpra.frame = hostFrameWitness{}
	}
	b.Surface.Geometry = Geometry{W: w, H: h}
	b.HostMinimized = false
	m.bindings[id] = b
	// Native identity profiles retain their configured next-create allocation.
	// Host-window profiles instead follow the witnessed opening geometry.
	if b.NativeKind == "host-window" || b.NativeKind == "host-xpra" {
		if p, ok := m.cfg.Profiles[b.Surface.ProfileID]; ok {
			p.Width = w
			p.Height = h
			m.cfg.Profiles[b.Surface.ProfileID] = p
		}
	}
	return changed
}

// SetHostWindowMinimized records whether the host window is currently minimized
// (waiting state; no old frame is served as live).
func (m *Manager) SetHostWindowMinimized(id string, minimized bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	b, ok := m.bindings[id]
	if !ok || b.Surface.State != "active" || m.destroying[id] || (b.NativeKind != "host-window" && b.NativeKind != "host-xpra") {
		return
	}
	if minimized && !b.HostMinimized && b.HostXpra != nil {
		b.FrameRefreshEpoch++
		b.HostXpra.frame = hostFrameWitness{}
	}
	b.HostMinimized = minimized
	m.bindings[id] = b
}

// MarkHostWindowLost transitions an active host-window surface to lost when
// the window closes or its witness changes, returning the state_changed event.
func (m *Manager) MarkHostWindowLost(id string, reason string) (json.RawMessage, error) {
	return m.markHostWindowLost(id, reason, false)
}

// BeginHostWindowLost coordinates DISPLAY loss delivery with VERIFIED error events.
func (m *Manager) BeginHostWindowLost(id string, reason string) (json.RawMessage, error) {
	return m.markHostWindowLost(id, reason, true)
}

func (m *Manager) markHostWindowLost(id string, reason string, trackDelivery bool) (json.RawMessage, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	b, ok := m.bindings[id]
	if !ok || (b.NativeKind != "host-window" && b.NativeKind != "host-xpra") {
		return nil, fault("surface", "host-window surface required")
	}
	if reason != "" {
		b.HostLostReason = extractOrDefaultHostWindowReason(reason, reason)
		m.bindings[id] = b
	}
	return m.markLostLocked(id, true, trackDelivery)
}
