// Package surface composes opaque identity leases, planned windows, exact RAM
// capture and focus-verified input. Capsule creation is a separate trusted port.
package surface

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/browserid/store"
	"opensuperficies.local/srf/src/browserid/tiling"
	"opensuperficies.local/srf/src/programsurface"
)

const (
	Image   = "opensuperficies-identity-browser-arm64:0.1.0"
	RAMRoot = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"
	Width   = 1280
	Height  = 900
	Display = ":100"
)

type Runner interface {
	Run(context.Context, []string, []byte) ([]byte, error)
}
type Clock func() uint64
type Error struct {
	Code   int
	Detail string
}

func (e *Error) Error() string   { return e.Detail }
func fail(c int, s string) error { return &Error{c, s} }
func Code(e error) int {
	if e == nil {
		return 0
	}
	var r *Error
	if errors.As(e, &r) && r.Code >= 2 && r.Code <= 7 {
		return r.Code
	}
	return 6
}

// Launch is private control-plane data. It must never be encoded to agent output.
// The Docker launcher receives the validated plan after private profile checks.
type Launch struct {
	Container string
	Argv      []string
}
type Launcher interface {
	Available() error
	Start(context.Context, Launch) error
	Remove(context.Context, string) error
}

// UnavailableLauncher is an explicit refusing adapter for unavailable runtimes.
// Production uses DockerLauncher under operator ruling 15. It remains useful
// to embedders that must reject launch before opening private state.
type UnavailableLauncher struct{}

func (UnavailableLauncher) Available() error {
	return fail(3, "AUSENTE — identity launch support in the osworker seat protocol")
}
func (u UnavailableLauncher) Start(context.Context, Launch) error  { return u.Available() }
func (u UnavailableLauncher) Remove(context.Context, string) error { return u.Available() }

type FrameSink interface {
	Write(runID, window string, seq int, rgb []byte, w, h int) (string, error)
}
type Tool struct {
	// Offline seams for host admission and readiness; nil uses the real host.
	vmHostRead    func(string) ([]byte, error)
	readinessWait func(context.Context) error
	// Private offline seams; production resolves the catalog only during Start.
	lookupProgram  func(string) (programsurface.Program, error)
	programRunArgs func(string, string, string, programsurface.Program, []programsurface.Tile, string) []string
	R              Runner
	Clock          Clock
	Root           string // private control state; frames always use the fixed RAM sink
	Store          *store.Store
	StoreRoot      string
	Registry       string
	Proxy          string
	Launcher       Launcher
	Listen         ListenFunc // nil uses a host loopback TCP listener
	jqueryPath     string     // offline test port; production uses the fixed host jquery file
	Sink           FrameSink
	S              Streamer // line streamer for demonstration recording; nil disables --record
	// ProfileCheck permits offline tests without changing/chowning opaque profiles.
	// Production leaves it nil and requires a real 0700 directory owned by uid 10001.
	ProfileCheck func(string) error

	// Offline seams for the open-egress firewall witness; production reads OpenWitness and lstat.
	witnessPath  string
	witnessOwner func(string) (uint32, error)
	openImage    bool // offline seam: treat Image as supporting open mode
}

func (t Tool) lookup(id string) (programsurface.Program, error) {
	if t.lookupProgram != nil {
		return t.lookupProgram(id)
	}
	return programsurface.Lookup(id)
}
func (t Tool) programArgs(c, profile, view string, p programsurface.Program, tiles []programsurface.Tile, proxy string) []string {
	if t.programRunArgs != nil {
		return t.programRunArgs(c, profile, view, p, tiles, proxy)
	}
	return programsurface.RunArgs(c, profile, view, p, tiles, proxy)
}

type StartSpec struct {
	Identity, Capsule string
	Kind, Program     string
	Windows           int
	// Net is empty (legacy) or a browser network mode; the rest apply to proxied only.
	Net, Policy, ProxyBin string
	Audit                 bool
}
type Window struct {
	ID    string `json:"id"`
	X     int    `json:"x"`
	Y     int    `json:"y"`
	W     int    `json:"w"`
	H     int    `json:"h"`
	Title string `json:"title"`
	URL   string `json:"url"`
}
type Capsule struct {
	Kind      string   `json:"kind,omitempty"`
	NetMode   string   `json:"net_mode,omitempty"` // absent: legacy
	Container string   `json:"container"`
	RunID     string   `json:"run_id"`
	Windows   []Window `json:"windows"`
}

// programLaunch is the immutable launch decision needed by later CLI calls.
// A pointer distinguishes an absent snapshot from a network-none decision.
type programLaunch struct {
	Networked       bool   `json:"networked"`
	ExchangeDir     string `json:"exchange_dir"`
	SeccompIdentity bool   `json:"seccomp_identity"`
	ClearSingleton  bool   `json:"clear_singleton"`
}

// Missing booleans are not equivalent to an explicit false launch decision.
func (p *programLaunch) UnmarshalJSON(b []byte) error {
	var wire struct {
		Networked       *bool  `json:"networked"`
		ExchangeDir     string `json:"exchange_dir"`
		SeccompIdentity *bool  `json:"seccomp_identity"`
		ClearSingleton  *bool  `json:"clear_singleton"`
	}
	if err := json.Unmarshal(b, &wire); err != nil {
		return err
	}
	if wire.Networked == nil || wire.SeccompIdentity == nil || wire.ClearSingleton == nil {
		return fail(6, "program launch decision unavailable")
	}
	*p = programLaunch{Networked: *wire.Networked, ExchangeDir: wire.ExchangeDir, SeccompIdentity: *wire.SeccompIdentity, ClearSingleton: *wire.ClearSingleton}
	return nil
}

type session struct {
	ProgramLaunch *programLaunch `json:"program_launch,omitempty"`
	Kind          string         `json:"kind,omitempty"`
	Program       string         `json:"program,omitempty"`
	NetMode       string         `json:"net_mode,omitempty"` // absent: legacy
	Capsule       Capsule
	Lease         store.Lease
	XIDs          map[string]string
	Seq           map[string]int
	Ready         bool
}

// BrowserMinWidth/Height bound identity window tiles (Chromium minimum width ~508 px).
const BrowserMinWidth, BrowserMinHeight = 640, 450

var browserCapsuleRE = regexp.MustCompile(`^srf-id-[a-z0-9][a-z0-9-]{0,39}$`)
var vmCapsuleRE = regexp.MustCompile(`^srf-vm-[a-z0-9][a-z0-9-]{0,39}$`)
var capsuleRE = regexp.MustCompile(`^srf-(id|pg|vm)-[a-z0-9][a-z0-9-]{0,39}$`)
var programIDRE = regexp.MustCompile(`^[a-z][a-z0-9-]{1,23}$`)
var identityRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,47}$`)
var windowRE = regexp.MustCompile(`^w([1-9]|1[0-6])$`)

func (t Tool) root() string {
	if t.Root != "" {
		return t.Root
	}
	return filepath.Join(RAMRoot, "identity-state")
}
func (t Tool) ports() error {
	if t.R == nil || t.Clock == nil || t.Store == nil || t.StoreRoot == "" || t.Registry == "" || t.Launcher == nil {
		return fail(3, "explicit runner, clock, store, registry and launcher required")
	}
	return nil
}

// lock covers all commands across processes, including the complete focus/input
// sequence and frame sequence allocation. It is context-bounded, never blocking flock.
func (t Tool) lock(ctx context.Context) (func(), error) {
	if ctx == nil || ctx.Err() != nil {
		return nil, fail(6, "operation cancelled")
	}
	root := t.root()
	if !filepath.IsAbs(root) {
		return nil, fail(3, "state root must be absolute")
	}
	if e := privateDir(root); e != nil {
		return nil, e
	}
	fd, e := unix.Open(filepath.Join(root, "control.lock"), unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0600)
	if e != nil {
		return nil, fail(4, "state lock unavailable")
	}
	for {
		e = unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB)
		if e == nil {
			return func() { _ = unix.Flock(fd, unix.LOCK_UN); _ = unix.Close(fd) }, nil
		}
		if e != unix.EWOULDBLOCK && e != unix.EAGAIN {
			_ = unix.Close(fd)
			return nil, fail(4, "state lock refused")
		}
		timer := time.NewTimer(10 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			_ = unix.Close(fd)
			return nil, fail(6, "state lock cancelled")
		case <-timer.C:
		}
	}
}
func privateDir(path string) error {
	// Refuse symlinks in every existing ancestor; do not traverse profile payloads.
	path = filepath.Clean(path)
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	cur := "/"
	for _, p := range parts {
		cur = filepath.Join(cur, p)
		fi, e := os.Lstat(cur)
		if os.IsNotExist(e) {
			if e = os.Mkdir(cur, 0700); e != nil && !os.IsExist(e) {
				return fail(4, "private state directory unavailable")
			}
			fi, e = os.Lstat(cur)
		}
		if e != nil || !fi.IsDir() || fi.Mode()&os.ModeSymlink != 0 {
			return fail(4, "unsafe state directory")
		}
	}
	fi, e := os.Lstat(path)
	if e != nil || fi.Mode().Perm() != 0700 {
		return fail(4, "state directory must be private 0700")
	}
	// os.FileInfo uses syscall.Stat_t; ownership is checked using the fd API.
	fd, e := unix.Open(path, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if e != nil {
		return fail(4, "state directory unavailable")
	}
	defer unix.Close(fd)
	var stat unix.Stat_t
	if unix.Fstat(fd, &stat) != nil || stat.Uid != uint32(os.Geteuid()) {
		return fail(4, "state directory owner mismatch")
	}
	return nil
}
func (t Tool) statePath(c string) string { return filepath.Join(t.root(), c+".json") }
func (t Tool) save(s session) error {
	// VM sessions use program behavior in memory, but preserve their wire identity on disk.
	if s.Kind == "program" && s.Capsule.Kind == "vm" && vmCapsuleRE.MatchString(s.Capsule.Container) {
		s.Kind = "vm"
	}
	return atomicJSON(t.statePath(s.Capsule.Container), s)
}
func (t Tool) load(c string) (session, error) {
	var s session
	if !capsuleRE.MatchString(c) {
		return s, fail(2, "invalid identity capsule")
	}
	b, e := readPrivate(t.statePath(c), 1<<20)
	if e != nil {
		return s, fail(4, "capsule state unavailable")
	}
	if json.Unmarshal(b, &s) != nil || s.Capsule.Container != c || s.Capsule.RunID != c || s.Lease.Holder != c || s.Lease.Token == "" || s.Lease.Counter == 0 || s.XIDs == nil || s.Seq == nil || !identityRE.MatchString(s.Lease.ProfileID) || len(s.Capsule.Windows) < 1 || len(s.Capsule.Windows) > 16 {
		return s, fail(6, "invalid capsule state")
	}
	if s.Kind == "" {
		s.Kind = "browser"
	}
	if s.NetMode != s.Capsule.NetMode || s.NetMode != "" && (s.Kind != "browser" || s.NetMode != NetOpen && s.NetMode != NetProxied) {
		return s, fail(6, "invalid capsule net mode")
	}
	if s.Kind == "browser" {
		if !browserCapsuleRE.MatchString(c) || s.Program != "" || s.Capsule.Kind != "" {
			return s, fail(6, "invalid browser state kind")
		}
	} else if s.Kind == "program" || s.Kind == "vm" {
		valid := programsurface.ValidCapsule(c)
		if s.Kind == "vm" {
			valid = vmCapsuleRE.MatchString(c)
		}
		if !valid || s.Capsule.Kind != s.Kind {
			return s, fail(6, "invalid program state kind")
		}
		if !programIDRE.MatchString(s.Program) || s.ProgramLaunch == nil {
			return s, fail(6, "program launch state unavailable")
		}
		folder := s.ProgramLaunch.ExchangeDir
		if !strings.HasPrefix(folder, "/profile/") || filepath.Clean(folder) != folder || strings.ContainsAny(folder, "\x00\r\n") {
			return s, fail(6, "invalid program exchange directory")
		}
	} else {
		return s, fail(6, "invalid capsule kind")
	}
	// Existing input, AX, download and view code consumes the program behavior.
	// Capsule.Kind remains "vm" for wire output; save restores the persisted VM kind.
	if s.Kind == "vm" {
		s.Kind = "program"
	}
	return s, nil
}
func atomicJSON(path string, v any) error {
	b, e := json.Marshal(v)
	if e != nil {
		return fail(6, "state encode")
	}
	parent := filepath.Dir(path)
	if e = privateDir(parent); e != nil {
		return e
	}
	f, e := os.CreateTemp(parent, ".identity-*")
	if e != nil {
		return fail(4, "state write unavailable")
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e == nil {
		e = os.Rename(name, path)
	}
	if e != nil {
		return fail(4, "state write failed")
	}
	return nil
}
func readPrivate(path string, cap int64) ([]byte, error) {
	fd, e := unix.Open(path, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	var st unix.Stat_t
	if unix.Fstat(fd, &st) != nil || st.Mode&unix.S_IFMT != unix.S_IFREG || st.Uid != uint32(os.Geteuid()) || st.Mode&0777 != 0600 || st.Size > cap {
		return nil, fail(4, "unsafe private state")
	}
	b := make([]byte, st.Size)
	n, e := f.ReadAt(b, 0)
	if n != len(b) {
		return nil, fail(4, "short state read")
	}
	return b, e
}
func (t Tool) profile(id string) (string, error) {
	if !filepath.IsAbs(t.StoreRoot) {
		return "", fail(3, "absolute store root required")
	}
	p := filepath.Join(t.StoreRoot, "profiles", id)
	if strings.ContainsAny(p, ",\n\r") {
		return "", fail(3, "profile mount path cannot contain mount delimiters")
	}
	if t.ProfileCheck != nil {
		return p, t.ProfileCheck(p)
	}
	// Only stat directory metadata; never open or enumerate the profile.
	resolved, e := filepath.EvalSymlinks(filepath.Dir(p))
	if e != nil || resolved != filepath.Clean(filepath.Dir(p)) {
		return "", fail(4, "unsafe profile parent")
	}
	var st unix.Stat_t
	if unix.Lstat(p, &st) != nil || st.Mode&unix.S_IFMT != unix.S_IFDIR || st.Mode&0777 != 0700 || st.Uid != 10001 {
		return "", fail(4, "profile must be private 0700 and owned by uid 10001")
	}
	return p, nil
}
func (t Tool) withRegistry(update func(map[string]string) error) error {
	// Registry ownership must match the CLI; the proxy only reads it. A separate
	// flock makes updates atomic across independent control roots as well.
	dir := filepath.Dir(t.Registry)
	if !filepath.IsAbs(t.Registry) {
		return fail(3, "absolute registry path required")
	}
	if e := privateDir(dir); e != nil {
		return e
	}
	fd, e := unix.Open(t.Registry+".lock", unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0600)
	if e != nil {
		return fail(4, "registry lock")
	}
	defer unix.Close(fd)
	if unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB) != nil {
		return fail(4, "registry busy")
	}
	defer unix.Flock(fd, unix.LOCK_UN)
	m := map[string]string{}
	b, e := readPrivate(t.Registry, 1<<20)
	if e != nil && !errors.Is(e, os.ErrNotExist) {
		return fail(4, "registry unavailable")
	}
	if e == nil && json.Unmarshal(b, &m) != nil {
		return fail(6, "registry corrupt")
	}
	if m == nil {
		return fail(6, "registry corrupt")
	}
	if e = update(m); e != nil {
		return e
	}
	return atomicJSON(t.Registry, m)
}
func (t Tool) registry(c, ip string, remove bool) error {
	return t.withRegistry(func(m map[string]string) error {
		if c == "" {
			return nil
		}
		if remove {
			delete(m, c)
		} else {
			for other, addr := range m {
				if other != c && addr == ip {
					return fail(6, "capsule address already registered")
				}
			}
			m[c] = ip
		}
		return nil
	})
}

// RunArgs describes the hardened launch. Profiles are bind mounted after metadata checks.
func RunArgs(c, profile, proxy string, windows []Window, viewDir string) []string {
	return runArgs(c, profile, "srf-idnet", []string{"SRF_EGRESS_PROXY=" + proxy}, windows, viewDir)
}

// runArgs is RunArgs with the network and the network env chosen by the net mode.
func runArgs(c, profile, network string, netEnv []string, windows []Window, viewDir string) []string {
	type tile struct {
		ID string `json:"id"`
		X  int    `json:"x"`
		Y  int    `json:"y"`
		W  int    `json:"w"`
		H  int    `json:"h"`
	}
	tiles := make([]tile, len(windows))
	for i, w := range windows {
		tiles[i] = tile{w.ID, w.X, w.Y, w.W, w.H}
	}
	b, _ := json.Marshal(tiles)
	a := []string{"docker", "run", "-d", "--name", c, "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--network", network, "--cpuset-cpus", "10-14", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256", "--shm-size", "2g", "--tmpfs", "/run/vqa/fb:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=256m", "--tmpfs", "/run/user/10001:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m", "--tmpfs", "/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m", "--tmpfs", "/home/worker:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m", "--mount", "type=bind,src=" + profile + ",dst=/profile", "--mount", "type=bind,src=" + viewDir + ",dst=/srfview"}
	for _, v := range netEnv {
		a = append(a, "--env", v)
	}
	return append(a, "--env", "SRF_WINDOWS="+strconv.Itoa(len(windows)), "--env", "SRF_TILES="+string(b), Image)
}

// programError preserves the frozen package's error code at the surface boundary.
func programError(e error) error {
	if e == nil {
		return nil
	}
	var pe *programsurface.Error
	if errors.As(e, &pe) {
		return fail(pe.Code, pe.Detail)
	}
	return fail(6, "program contract failed")
}
func programTiles(windows []Window) []programsurface.Tile {
	tiles := make([]programsurface.Tile, len(windows))
	for i, w := range windows {
		tiles[i] = programsurface.Tile{ID: w.ID, X: w.X, Y: w.Y, W: w.W, H: w.H}
	}
	return tiles
}
func ValidateStartSpec(spec StartSpec) error {
	kind := spec.Kind
	if kind == "" {
		kind = "browser"
	}
	if kind != "browser" && kind != "program" && kind != "vm" {
		return fail(2, "invalid capsule kind")
	}
	if (kind == "program" || kind == "vm") != (spec.Program != "") {
		return fail(2, "program id requires kind program or vm")
	}
	validCapsule := spec.Capsule == "" || browserCapsuleRE.MatchString(spec.Capsule)
	if kind == "program" {
		validCapsule = spec.Capsule == "" || programsurface.ValidCapsule(spec.Capsule)
	}
	if kind == "vm" {
		validCapsule = spec.Capsule == "" || vmCapsuleRE.MatchString(spec.Capsule)
	}
	if !identityRE.MatchString(spec.Identity) || spec.Windows < 1 || spec.Windows > 16 || !validCapsule {
		return fail(2, "invalid identity, capsule or windows")
	}
	return validateNet(kind, spec)
}
func (t Tool) Start(ctx context.Context, spec StartSpec) (Capsule, error) {
	var out Capsule
	if e := ValidateStartSpec(spec); e != nil {
		return out, e
	}
	if e := t.ports(); e != nil {
		return out, e
	}
	if spec.Kind == "" {
		spec.Kind = "browser"
	}
	var program programsurface.Program
	networked, seccomp, singleton := true, true, true
	minWidth, minHeight := BrowserMinWidth, BrowserMinHeight
	if spec.Kind == "program" || spec.Kind == "vm" {
		var err error
		program, err = t.lookup(spec.Program)
		if err != nil {
			return out, programError(err)
		}
		if (spec.Kind == "vm") != (program.Runtime == programsurface.RuntimeVM) {
			return out, fail(2, "program runtime and capsule kind mismatch")
		}
		if program.FixedWidth != 0 || program.FixedHeight != 0 {
			if program.FixedWidth <= 0 || program.FixedHeight <= 0 || program.MaxWindows != 1 || spec.Windows != 1 {
				return out, fail(2, "fixed program geometry requires both dimensions and one window")
			}
			if program.FixedWidth < program.MinWidth || program.FixedWidth > Width || program.FixedHeight < program.MinHeight || program.FixedHeight > Height {
				return out, fail(2, "fixed program geometry outside minimum or display bounds")
			}
		}
		if err = program.Validate(); err != nil {
			return out, programError(err)
		}
		if spec.Windows > program.MaxWindows {
			return out, fail(2, "program window count exceeded")
		}
		networked = program.Network == programsurface.NetProxy
		seccomp, singleton = program.SeccompIdentity, program.ClearSingleton
		minWidth, minHeight = program.MinWidth, program.MinHeight
	}
	var kvmGID string
	if spec.Kind == "vm" {
		var err error
		kvmGID, err = t.vmAdmission()
		if err != nil {
			return out, err
		}
	}
	mode := spec.Net
	if mode == "" {
		mode = NetLegacy
	}
	legacy := networked && mode == NetLegacy
	if legacy && t.Proxy != ProxyURL {
		return out, fail(3, "fixed internal HTTP egress proxy required")
	}
	// Fail closed before any lease, network or docker call.
	if e := t.netPreflight(spec); e != nil {
		return out, e
	}
	var e error
	if e = t.Launcher.Available(); e != nil {
		return out, e
	}
	unlock, e := t.lock(ctx)
	if e != nil {
		return out, e
	}
	defer unlock()
	if legacy {
		if e = t.proxyRunning(ctx); e != nil {
			return out, e
		}
		if e = t.verifyNetwork(ctx); e != nil {
			return out, e
		}
	}
	if spec.Capsule == "" {
		var b [6]byte
		if _, e = rand.Read(b[:]); e != nil {
			return out, fail(6, "capsule identifier entropy unavailable")
		}
		prefix := "srf-id-"
		if spec.Kind == "program" || spec.Kind == "vm" {
			prefix = programsurface.CapsulePrefix
		}
		if spec.Kind == "vm" {
			prefix = programsurface.VMCapsulePrefix
		}
		spec.Capsule = prefix + hex.EncodeToString(b[:])
	}
	if _, e = os.Lstat(t.statePath(spec.Capsule)); !os.IsNotExist(e) {
		return out, fail(4, "capsule name already reserved")
	}
	// Chromium refuses windows narrower than ~508 px (measured live 2026-10-23: a 426 px tile
	// stayed 508 wide and readiness never verified); 640x450 tiles fit up to 4 windows in 1280x900.
	var layout tiling.Layout
	if (spec.Kind == "program" || spec.Kind == "vm") && program.FixedWidth != 0 && program.FixedHeight != 0 {
		layout.Windows = []tiling.Window{{SurfaceID: "w1", Rect: tiling.Rect{Width: program.FixedWidth, Height: program.FixedHeight}}}
	} else {
		layout, e = tiling.Plan(tiling.Spec{ProfileID: spec.Identity, DisplayWidth: Width, DisplayHeight: Height, Windows: spec.Windows, MinWidth: minWidth, MinHeight: minHeight})
		if e != nil {
			return out, fail(2, "tile plan refused")
		}
	}
	identity, err := t.Store.Get(spec.Identity)
	if err != nil {
		return out, fail(4, "identity unavailable")
	}
	if (identity.Browser == "program") != (spec.Kind == "program" || spec.Kind == "vm") {
		return out, fail(4, "identity kind mismatch")
	}
	profile, e := t.profile(spec.Identity)
	if e != nil {
		return out, e
	}
	lease, e := t.Store.Acquire(spec.Identity, spec.Capsule)
	if e != nil {
		return out, fail(4, "identity unavailable or exclusively held")
	}
	out = Capsule{Container: spec.Capsule, RunID: spec.Capsule, Windows: make([]Window, len(layout.Windows))}
	for i, w := range layout.Windows {
		out.Windows[i] = Window{ID: fmt.Sprintf("w%d", i+1), X: w.Rect.X, Y: w.Rect.Y, W: w.Rect.Width, H: w.Rect.Height}
	}
	if spec.Kind == "program" || spec.Kind == "vm" {
		out.Kind = spec.Kind
	}
	ip := ""
	if legacy {
		ip, e = t.reserveIP(out.Container)
		if e != nil {
			_ = t.Store.Release(lease)
			return out, e
		}
	}
	var plan netPlan
	if !legacy && networked {
		// netModeUp removes whatever it created when it fails.
		if plan, e = t.netModeUp(ctx, out.Container, spec); e != nil {
			_ = t.Store.Release(lease)
			return out, e
		}
		out.NetMode = mode
	}
	cleanupRegistry := func() {
		if legacy {
			_ = t.registry(out.Container, "", true)
		} else if networked {
			cleanup, done := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
			_ = t.netModeDown(cleanup, out.Container, mode)
			done()
		}
	}
	state := session{Capsule: out, Kind: spec.Kind, Program: spec.Program, NetMode: out.NetMode, Lease: lease, XIDs: map[string]string{}, Seq: map[string]int{}}
	if spec.Kind == "program" || spec.Kind == "vm" {
		state.ProgramLaunch = &programLaunch{Networked: networked, ExchangeDir: program.ExchangeDir, SeccompIdentity: seccomp, ClearSingleton: singleton}
	}
	if e = t.save(state); e != nil {
		cleanupRegistry()
		_ = t.Store.Release(lease)
		return out, e
	}
	// From here an ambiguous launch retains its lease and address for stop.
	// Chromium leaves SingletonLock/Cookie/Socket links naming the previous capsule's hostname;
	// the next capsule then refuses the profile ("in use by another computer") and never maps a
	// window. The lease makes this holder exclusive, so the stale links are safe to remove.
	if singleton {
		if _, e = t.R.Run(ctx, ClearSingletonArgs(profile), nil); e != nil {
			cleanupRegistry()
			_ = t.Store.Release(lease)
			return out, fail(6, "stale browser lock cleanup failed")
		}
	}
	// Frames of a previous run of this capsule name would collide with the reset sequence.
	if e = os.RemoveAll(filepath.Join(RAMRoot, out.Container, "frames")); e != nil {
		cleanupRegistry()
		_ = t.Store.Release(lease)
		return out, fail(4, "stale RAM frames could not be cleared")
	}
	var extra []string
	if legacy {
		extra = append(extra, "--ip", ip)
	} else if plan.CapsuleIP != "" {
		extra = append(extra, "--ip", plan.CapsuleIP)
	}
	if seccomp {
		sec, err := t.writeSeccomp()
		if err != nil {
			cleanupRegistry()
			_ = t.Store.Release(lease)
			return out, err
		}
		extra = append(extra, "--security-opt", "seccomp="+sec)
	}
	if e = t.createViewDir(out.Container); e != nil {
		if !legacy {
			cleanupRegistry()
		}
		return out, e
	}
	args := RunArgs(out.Container, profile, t.Proxy, out.Windows, filepath.Dir(t.viewSocket(out.Container)))
	if !legacy && networked {
		args = runArgs(out.Container, profile, plan.Network, plan.Env, out.Windows, filepath.Dir(t.viewSocket(out.Container)))
	}
	if spec.Kind == "program" || spec.Kind == "vm" {
		proxy := ""
		if networked {
			proxy = t.Proxy
		}
		if spec.Kind == "vm" {
			args = t.vmProgramArgs(kvmGID, out.Container, profile, filepath.Dir(t.viewSocket(out.Container)), program, programTiles(out.Windows), proxy)
		} else {
			args = t.programArgs(out.Container, profile, filepath.Dir(t.viewSocket(out.Container)), program, programTiles(out.Windows), proxy)
		}
	}
	if len(args) < 5 {
		return out, fail(3, "program launch arguments refused")
	}
	args = append(args[:5], append(extra, args[5:]...)...)
	if e = t.Launcher.Start(ctx, Launch{out.Container, args}); e != nil {
		return out, fail(6, "launch indeterminate; lease retained; stop capsule to recover")
	}
	if legacy {
		actual, e := t.R.Run(ctx, []string{"docker", "inspect", "--format", `{{(index .NetworkSettings.Networks "srf-idnet").IPAddress}}`, out.Container}, nil)
		if e != nil || strings.TrimSpace(string(actual)) != ip {
			return out, fail(6, "capsule address mismatch; lease retained")
		}
	} else if networked {
		if e = t.verifyCapsuleNet(ctx, out.Container, plan); e != nil {
			return out, e
		}
	}
	ready := false
	xpraReady := false
	var reported map[string]string
	attempts, tail := readinessBudget(spec.Kind)
	for attempt := 0; attempt < attempts; attempt++ {
		logs, err := t.R.Run(ctx, []string{"docker", "logs", "--tail", tail, out.Container}, nil)
		if err != nil {
			return out, fail(4, "capsule readiness unavailable")
		}
		for _, line := range strings.Split(string(logs), "\n") {
			if line == "xpra layer up" {
				xpraReady = true
			} else if (spec.Kind == "program" || spec.Kind == "vm") && strings.HasPrefix(line, programsurface.ReadyToken) {
				if !strings.HasPrefix(line, programsurface.ReadyToken+" {") {
					return out, fail(6, "invalid program readiness separator")
				}
				windows, err := programsurface.ParseReady(line, programTiles(out.Windows))
				if err != nil {
					return out, programError(err)
				}
				reported = map[string]string{}
				for _, w := range windows {
					id, err := xid(w.XID)
					if err != nil {
						return out, err
					}
					reported[w.ID] = id
				}
				ready = true
			} else if spec.Kind == "program" || spec.Kind == "vm" {
				continue
			} else if line == "IDENTITY_READY" {
				ready = true
			} else if strings.HasPrefix(line, "IDENTITY_READY ") {
				var report struct {
					Windows []struct {
						ID  string `json:"id"`
						X   int    `json:"x"`
						Y   int    `json:"y"`
						W   int    `json:"w"`
						H   int    `json:"h"`
						XID string `json:"xid"`
					} `json:"windows"`
				}
				if json.Unmarshal([]byte(strings.TrimPrefix(line, "IDENTITY_READY ")), &report) != nil || len(report.Windows) != len(out.Windows) {
					return out, fail(6, "invalid readiness window map")
				}
				reported = map[string]string{}
				for i, v := range report.Windows {
					expected := out.Windows[i]
					actual := Window{ID: v.ID, X: v.X, Y: v.Y, W: v.W, H: v.H}
					if actual.ID != expected.ID || !sameRect(actual, expected) {
						return out, fail(6, "readiness tile mismatch")
					}
					id, err := xid(v.XID)
					if err != nil {
						return out, err
					}
					reported[v.ID] = id
				}
				ready = true
			}
		}
		if ready && xpraReady {
			break
		}
		if err := t.waitReadiness(ctx); err != nil {
			return out, err
		}
	}
	if !ready || !xpraReady {
		return out, fail(4, "readiness timeout; lease retained")
	}
	// The capsule reports each window's X id; verify every one against its live geometry
	// (the base image has no wmctrl, so there is no window list to match against).
	if reported == nil {
		return out, fail(6, "readiness X window map missing")
	}
	for _, w := range out.Windows {
		x, ok := reported[w.ID]
		if !ok {
			return out, fail(6, "readiness X window mismatch")
		}
		live, ge := t.geometry(ctx, out.Container, x)
		if ge != nil {
			return out, ge
		}
		if !sameRect(Window{ID: w.ID, X: live.X, Y: live.Y, W: live.W, H: live.H}, w) {
			return out, fail(6, "planned windows not verified")
		}
		state.XIDs[w.ID] = x
	}
	state.Ready = true
	if e = t.save(state); e != nil {
		return out, e
	}
	return out, nil
}
func (t Tool) exec(ctx context.Context, c string, args ...string) ([]byte, error) {
	a := []string{"docker", "exec", "-u", "10001:10001", "-e", "DISPLAY=" + Display, c, "timeout", "--signal=TERM", "--kill-after=1s", "5s"}
	a = append(a, args...)
	b, e := t.R.Run(ctx, a, nil)
	if errors.Is(e, exec.ErrNotFound) {
		return nil, fail(4, "docker not found on PATH")
	}
	if e != nil {
		return nil, fail(6, "capsule command failed")
	}
	return b, nil
}
func (t Tool) Stop(ctx context.Context, c string) error {
	if e := t.ports(); e != nil {
		return e
	}
	unlock, e := t.lock(ctx)
	if e != nil {
		return e
	}
	defer unlock()
	s, e := t.load(c)
	if e != nil {
		return e
	}
	if e = t.Launcher.Remove(ctx, c); e != nil {
		return fail(6, "capsule removal indeterminate; lease retained")
	}
	b, e := t.R.Run(ctx, []string{"docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=^/" + c + "$"}, nil)
	if e != nil || strings.TrimSpace(string(b)) != "" {
		return fail(6, "removal unverified; lease retained")
	}
	if e = os.RemoveAll(filepath.Dir(filepath.Dir(t.viewSocket(c)))); e != nil {
		return fail(6, "stopped view cleanup failed")
	}
	networked := s.Kind == "browser" || s.ProgramLaunch.Networked
	if networked && s.NetMode == "" {
		if e = t.registry(c, "", true); e != nil {
			return e
		}
	} else if s.NetMode != "" {
		// Order: capsule (above), per-capsule proxy, per-capsule network, subnet reservation.
		if e = t.netModeDown(ctx, c, s.NetMode); e != nil {
			return e
		}
	}
	if e = t.Store.Release(s.Lease); e != nil {
		return fail(6, "identity release failed")
	}
	if os.Remove(t.statePath(c)) != nil {
		return fail(6, "stopped state cleanup failed")
	}
	return nil
}

// ClearSingletonArgs removes Chromium's per-host singleton links from an identity profile, as
// uid 10001, with no network, no capabilities and a read-only root.
func ClearSingletonArgs(profile string) []string {
	return []string{"docker", "run", "--rm", "--user", "10001:10001", "--network", "none", "--read-only", "--cap-drop", "ALL",
		"--mount", "type=bind,src=" + profile + ",dst=/profile", "--entrypoint", "/bin/rm", Image,
		"-f", "/profile/SingletonLock", "/profile/SingletonCookie", "/profile/SingletonSocket"}
}

// readinessBudget keeps the original container budget and widens only VM boot.
func readinessBudget(kind string) (int, string) {
	if kind == "vm" {
		return 600, "60"
	}
	return 100, "30"
}
func (t Tool) waitReadiness(ctx context.Context) error {
	if t.readinessWait != nil {
		return t.readinessWait(ctx)
	}
	timer := time.NewTimer(100 * time.Millisecond)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return fail(6, "readiness cancelled; lease retained")
	case <-timer.C:
		return nil
	}
}
func (t Tool) vmAdmission() (string, error) {
	read := t.vmHostRead
	if read == nil {
		read = os.ReadFile
	}
	groups, err := read("/etc/group")
	if err != nil {
		return "", fail(3, "kvm group unavailable")
	}
	gid := ""
	for _, line := range strings.Split(string(groups), "\n") {
		fields := strings.Split(line, ":")
		if len(fields) == 4 && fields[0] == "kvm" {
			if gid != "" {
				return "", fail(3, "invalid kvm group")
			}
			parsedGID, err := strconv.ParseUint(fields[2], 10, 32)
			if !decimalGID.MatchString(fields[2]) || err != nil || parsedGID == 0 || parsedGID > 4294967294 {
				return "", fail(3, "kvm group gid invalid")
			}
			gid = fields[2]
		}
	}
	if gid == "" {
		return "", fail(3, "kvm group unavailable")
	}
	memory, err := read("/proc/meminfo")
	if err != nil {
		return "", fail(3, "VM memory admission unavailable")
	}
	for _, line := range strings.Split(string(memory), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 3 && fields[0] == "MemAvailable:" && fields[2] == "kB" {
			available, err := strconv.ParseUint(fields[1], 10, 64)
			if err == nil && available >= 16*1024*1024 {
				return gid, nil
			}
			break
		}
	}
	return "", fail(3, "VM requires MemAvailable >= 16 GiB")
}

var decimalGID = regexp.MustCompile(`^[0-9]+$`)
var vmEnvironmentMu sync.Mutex

// The frozen RunArgs API consumes SRF_KVM_GID from the process environment.
// Serialize the short scoped override and restore the caller's environment.
func (t Tool) vmProgramArgs(gid, c, profile, view string, p programsurface.Program, tiles []programsurface.Tile, proxy string) []string {
	vmEnvironmentMu.Lock()
	defer vmEnvironmentMu.Unlock()
	previous, exists := os.LookupEnv("SRF_KVM_GID")
	// Temporary until programsurface.RunArgs takes the gid explicitly (frozen API; planned).
	if os.Setenv("SRF_KVM_GID", gid) != nil {
		return nil
	}
	defer func() {
		if exists {
			_ = os.Setenv("SRF_KVM_GID", previous)
		} else {
			_ = os.Unsetenv("SRF_KVM_GID")
		}
	}()
	return t.programArgs(c, profile, view, p, tiles, proxy)
}
