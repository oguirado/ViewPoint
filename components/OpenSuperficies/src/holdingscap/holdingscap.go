// Package holdingscap drives Holdings test capsules for computer-use work:
// start N desktop capsules running the Holdings test build, take exact frames
// (grab-held pair, canonical RGB24 PNG written to RAM only), inject XTEST input
// with an in-capsule monotonic stamp, read the agent-lane state, and stop.
//
// Frames never touch disk (STORAGE LAW): PNGs go under the RAM workspace
// /dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run_id>/frames.
package holdingscap

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"image"
	"image/png"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"opensuperficies.local/srf/src/ramcap"
)

// Image is the desktop capsule image (Xvfb -fbdir -nocursor, openbox, AT-SPI, pinned srf-fbread).
const Image = "opensuperficies-desktop-arm64:1.1.0"

// RAMRoot is the only frame root (U-RUL-0007, STORAGE LAW).
const RAMRoot = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"

// Fixed capsule geometry and in-capsule paths.
const (
	Width      = 1280
	Height     = 900
	Display    = ":100"
	LaneSocket = "/workspace/holdings/agent.sock"
	stdoutLog  = "/workspace/holdings/stdout.log"
	fbread     = "/usr/local/libexec/srf-fbread"
	prefix     = "srf-ng-hcap-"
)

// Runner runs host commands (docker). Tests inject a fake.
type Runner interface {
	Run(ctx context.Context, argv []string, stdin []byte) (stdout []byte, err error)
}

// Clock gives CLOCK_MONOTONIC nanoseconds; tests inject a fake.
type Clock func() uint64

// Error is a refusal with an exit code in 2..7.
type Error struct {
	Code   int
	Detail string
}

func (e *Error) Error() string { return e.Detail }

func fail(code int, format string, a ...any) error {
	return &Error{Code: code, Detail: fmt.Sprintf(format, a...)}
}

// Code maps an error to the CLI exit code (never 1).
func Code(e error) int {
	if e == nil {
		return 0
	}
	if r, ok := e.(*Error); ok && r.Code >= 2 && r.Code <= 7 {
		return r.Code
	}
	return 6
}

var (
	runIDRe     = regexp.MustCompile(`^hc-[0-9a-f]{12}$`)
	containerRe = regexp.MustCompile(`^srf-ng-hcap-hc-[0-9a-f]{12}-[0-9]{1,2}$`)
	scenarioRe  = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)
	keysymRe    = regexp.MustCompile(`^[A-Za-z0-9_+]{1,32}$`)
)

// StartSpec selects the Holdings build, scenario and number of capsules.
type StartSpec struct {
	Slots    int
	AppDir   string // host folder of the Holdings linux-arm64 build, readable by uid 10001
	Scenario string
	Seed     int64
	CPUs     string // docker --cpuset-cpus, default 10-14
	UI       string // "" (app default, lit) or the app's closed --ui set: "lit" or "v2"
}

// Capsule describes one started capsule.
type Capsule struct {
	Slot       int    `json:"slot"`
	Container  string `json:"container"`
	RunID      string `json:"run_id"`
	Display    string `json:"display"`
	Width      int    `json:"width"`
	Height     int    `json:"height"`
	LaneSocket string `json:"lane_socket"`
	ReadyLine  string `json:"ready_line"`
	ReadyMS    int64  `json:"ready_ms"`
	FramesDir  string `json:"frames_dir"`
}

// Tool bundles the injected dependencies.
type Tool struct {
	R     Runner
	Clock Clock
	Root  string // RAM frame root; defaults to RAMRoot
	Sleep func(time.Duration)
}

func (t Tool) root() string {
	if t.Root != "" {
		return t.Root
	}
	return RAMRoot
}
func (t Tool) sleep(d time.Duration) {
	if t.Sleep != nil {
		t.Sleep(d)
		return
	}
	time.Sleep(d)
}

func newRunID() (string, error) {
	b := make([]byte, 6)
	if _, e := rand.Read(b); e != nil {
		return "", e
	}
	return "hc-" + hex.EncodeToString(b), nil
}

// RunArgs is the fixed docker run argv for one capsule (no network, 4 GiB, read-only app).
func RunArgs(container, appDir, cpus string) []string {
	return []string{"docker", "run", "-d", "--name", container, "--label", "srf.holdingscap=1",
		"--user", "10001:0", "--memory", "4g", "--memory-swap", "4g", "--cpuset-cpus", cpus,
		"--network", "none", "--shm-size", "2g",
		"--tmpfs", "/run/vqa/fb:rw,nosuid,nodev,noexec,mode=0700,uid=10001,size=5656576",
		"--tmpfs", "/workspace:rw,exec,nosuid,nodev,mode=0770,uid=10001,size=1g",
		"-v", appDir + ":/opt/holdings:ro", Image}
}

// LaunchCommand starts Holdings inside the capsule. --no-sandbox is allowed only in
// this test capsule (operator decision D-AL1, 2026-10-22).
// ui is "" or a validated value of the app's closed --ui set (lit|v2).
func LaunchCommand(scenario string, seed int64, ui string) string {
	uiFlag := ""
	if ui != "" {
		uiFlag = " --ui " + ui
	}
	return fmt.Sprintf("mkdir -p /workspace/holdings && DISPLAY=%s LIBGL_ALWAYS_SOFTWARE=1 /opt/holdings/holdings-h2l --no-sandbox "+
		"--user-data-dir=/workspace/holdings-profile --holdings-test-mode --seed %d --scenario %s --window %dx%d%s "+
		"--agent-lane-socket %s > %s 2>&1", Display, seed, scenario, Width, Height, uiFlag, LaneSocket, stdoutLog)
}

// Start launches spec.Slots capsules and waits for HOLDINGS_READY in each.
func (t Tool) Start(ctx context.Context, spec StartSpec) (caps []Capsule, err error) {
	if spec.Slots < 1 || spec.Slots > 4 {
		return nil, fail(2, "slots must be 1..4 (measured ceiling; operator ruling)")
	}
	if spec.UI != "" && spec.UI != "lit" && spec.UI != "v2" {
		return nil, fail(2, "--ui must be lit or v2")
	}
	if !scenarioRe.MatchString(spec.Scenario) || spec.Seed < 0 {
		return nil, fail(2, "scenario or seed")
	}
	if spec.CPUs == "" {
		spec.CPUs = "10-14"
	}
	st, e := os.Stat(filepath.Join(spec.AppDir, "holdings-h2l"))
	dir, de := os.Stat(spec.AppDir)
	if e != nil || st.Mode().Perm()&0o005 != 0o005 || de != nil || dir.Mode().Perm()&0o005 != 0o005 {
		return nil, fail(4, "app folder unreadable for the capsule user: %s needs holdings-h2l readable+executable by others (stage a copy with chmod -R a+rX)", spec.AppDir)
	}
	runID, e := newRunID()
	if e != nil {
		return nil, fail(3, "run id: %v", e)
	}
	frames := filepath.Join(t.root(), runID, "frames")
	if e = os.MkdirAll(frames, 0o700); e != nil {
		return nil, fail(4, "RAM frame root: %v", e)
	}
	// A start that fails part-way removes every capsule it created: callers get no live leftovers.
	defer func() {
		if err != nil {
			for _, c := range caps {
				_ = t.Stop(context.WithoutCancel(ctx), c.Container)
			}
			caps = nil
		}
	}()
	for i := 1; i <= spec.Slots; i++ {
		c := fmt.Sprintf("%s%s-%d", prefix, runID, i)
		if _, e = t.R.Run(ctx, RunArgs(c, spec.AppDir, spec.CPUs), nil); e != nil {
			caps = append(caps, Capsule{Slot: i, Container: c}) // docker run may have created it before failing
			return caps, fail(3, "docker run %s: %v", c, e)
		}
		caps = append(caps, Capsule{Slot: i, Container: c, RunID: runID, Display: Display, Width: Width, Height: Height, LaneSocket: LaneSocket, FramesDir: filepath.Join(frames, strconv.Itoa(i))})
	}
	for i := range caps {
		if e = t.waitLog(ctx, caps[i].Container, "xpra layer up", 40*time.Second); e != nil {
			return caps, e
		}
		t0 := t.Clock()
		if _, e = t.R.Run(ctx, []string{"docker", "exec", "-d", "-u", "10001", caps[i].Container, "sh", "-c", LaunchCommand(spec.Scenario, spec.Seed, spec.UI)}, nil); e != nil {
			return caps, fail(3, "launch %s: %v", caps[i].Container, e)
		}
		ready := fmt.Sprintf("HOLDINGS_READY %d 0", spec.Seed)
		if e = t.waitReady(ctx, caps[i].Container, ready, 60*time.Second); e != nil {
			return caps, e
		}
		caps[i].ReadyLine = ready
		caps[i].ReadyMS = int64(t.Clock()-t0) / 1e6
	}
	return caps, nil
}

func (t Tool) waitLog(ctx context.Context, c, needle string, limit time.Duration) error {
	for waited := time.Duration(0); waited < limit; waited += 250 * time.Millisecond {
		// The capsule entrypoint logs to stderr; c is a validated capsule name.
		out, _ := t.R.Run(ctx, []string{"sh", "-c", "docker logs " + c + " 2>&1"}, nil)
		if bytes.Contains(out, []byte(needle)) {
			return nil
		}
		t.sleep(250 * time.Millisecond)
	}
	return fail(4, "%s: %q not seen", c, needle)
}

func (t Tool) waitReady(ctx context.Context, c, line string, limit time.Duration) error {
	for waited := time.Duration(0); waited < limit; waited += 100 * time.Millisecond {
		out, _ := t.R.Run(ctx, []string{"docker", "exec", "-u", "10001", c, "cat", stdoutLog}, nil)
		for _, l := range strings.Split(string(out), "\n") {
			if l == line {
				return nil
			}
		}
		t.sleep(100 * time.Millisecond)
	}
	return fail(4, "%s: %q not seen", c, line)
}

func checkContainer(c string) (runID string, err error) {
	if !containerRe.MatchString(c) {
		return "", fail(2, "container %q is not a holdingscap capsule", c)
	}
	rest := strings.TrimPrefix(c, prefix)
	return rest[:strings.LastIndexByte(rest, '-')], nil
}

// Frame is one exact captured frame stored in RAM.
type Frame struct {
	Path          string `json:"path"`
	Seq           int    `json:"seq"`
	SHA256RGB24   string `json:"sha256_rgb24"`
	Width         int    `json:"width"`
	Height        int    `json:"height"`
	CaptureMonoNS uint64 `json:"capture_mono_ns"`
	Generation    string `json:"generation"`
	State         any    `json:"state,omitempty"`
}

// Frame takes one grab-held pair, checks both legs agree bit-exactly, and writes the
// canonical RGB24 as PNG under the run's RAM frames dir. capture_mono_ns is the host
// CLOCK_MONOTONIC stamp taken right before the capture exec (shared with the capsule).
func (t Tool) Frame(ctx context.Context, c string, withState bool) (Frame, error) {
	var f Frame
	runID, e := checkContainer(c)
	if e != nil {
		return f, e
	}
	if !runIDRe.MatchString(runID) {
		return f, fail(2, "run id")
	}
	// One sequence per capsule: frames/<slot>/fNNNNNN.png.
	dir := filepath.Join(t.root(), runID, "frames", c[strings.LastIndexByte(c, '-')+1:])
	if e = os.MkdirAll(dir, 0o700); e != nil {
		return f, fail(4, "RAM frame root: %v", e)
	}
	stamp := t.Clock()
	raw, e := t.R.Run(ctx, []string{"docker", "exec", "-u", "10001", c, fbread, "pair", Display, "0"}, nil)
	if e != nil {
		return f, fail(5, "pair capture: %v", e)
	}
	legs, e := splitPair(raw)
	if e != nil {
		return f, e
	}
	rgb, sha, e := ramcap.PairLegs(legs[0], legs[1], Width, Height)
	if e != nil {
		return f, fail(5, "pair legs differ or invalid: %v", e)
	}
	seq := nextSeq(dir)
	path := filepath.Join(dir, fmt.Sprintf("f%06d.png", seq))
	if e = writePNG(path, rgb, Width, Height); e != nil {
		return f, fail(4, "write RAM frame: %v", e)
	}
	f = Frame{Path: path, Seq: seq, SHA256RGB24: hex.EncodeToString(sha[:]), Width: Width, Height: Height, CaptureMonoNS: stamp, Generation: c}
	if withState {
		obs, e := t.Observe(ctx, c)
		if e != nil {
			return f, e
		}
		f.State = obs
	}
	return f, nil
}

func splitPair(raw []byte) ([2][]byte, error) {
	var legs [2][]byte
	for i := 0; i < 2; i++ {
		if len(raw) < 8 {
			return legs, fail(5, "pair framing")
		}
		n := binary.BigEndian.Uint64(raw[:8])
		if n > uint64(len(raw)-8) {
			return legs, fail(5, "pair framing length")
		}
		legs[i] = raw[8 : 8+n]
		raw = raw[8+n:]
	}
	if len(raw) != 0 {
		return legs, fail(5, "pair trailing bytes")
	}
	return legs, nil
}

func nextSeq(dir string) int {
	entries, _ := os.ReadDir(dir)
	n := 0
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "f") && strings.HasSuffix(e.Name(), ".png") {
			if v, err := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(e.Name(), "f"), ".png")); err == nil && v > n {
				n = v
			}
		}
	}
	return n + 1
}

func writePNG(path string, rgb []byte, w, h int) error {
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for i := 0; i < w*h; i++ {
		img.Pix[4*i], img.Pix[4*i+1], img.Pix[4*i+2], img.Pix[4*i+3] = rgb[3*i], rgb[3*i+1], rgb[3*i+2], 255
	}
	var b bytes.Buffer
	if e := png.Encode(&b, img); e != nil {
		return e
	}
	tmp := path + ".tmp"
	if e := os.WriteFile(tmp, b.Bytes(), 0o600); e != nil {
		return e
	}
	return os.Rename(tmp, path)
}

// Input injects one XTEST key or click inside the capsule and returns the in-capsule
// CLOCK_MONOTONIC stamp taken right before injection (includes xdotool start-up).
type Input struct {
	Key    string
	X, Y   int
	Button int
}

// InputResult carries the injection stamp.
type InputResult struct {
	InputMonoNS uint64 `json:"input_mono_ns"`
}

func (t Tool) Input(ctx context.Context, c string, in Input) (InputResult, error) {
	var r InputResult
	if _, e := checkContainer(c); e != nil {
		return r, e
	}
	var args string
	switch {
	case in.Key != "":
		if !keysymRe.MatchString(in.Key) {
			return r, fail(2, "keysym %q", in.Key)
		}
		args = fmt.Sprintf("['xdotool','key','--clearmodifiers','%s']", in.Key)
	default:
		if in.X < 0 || in.Y < 0 || in.X >= Width || in.Y >= Height || in.Button < 1 || in.Button > 3 {
			return r, fail(2, "click outside %dx%d or bad button", Width, Height)
		}
		args = fmt.Sprintf("['xdotool','mousemove','--sync','%d','%d','click','%d']", in.X, in.Y, in.Button)
	}
	script := "import time,subprocess,sys;t=time.monotonic_ns();p=subprocess.run(" + args + ");print(t);sys.exit(0 if p.returncode==0 else 5)"
	out, e := t.R.Run(ctx, []string{"docker", "exec", "-u", "10001", "-e", "DISPLAY=" + Display, c, "python3", "-I", "-c", script}, nil)
	if e != nil {
		return r, fail(5, "xdotool: %v", e)
	}
	v, e := strconv.ParseUint(strings.TrimSpace(string(out)), 10, 64)
	if e != nil {
		return r, fail(5, "input stamp %q", strings.TrimSpace(string(out)))
	}
	r.InputMonoNS = v
	return r, nil
}

// MaxLaneReply bounds one agent-lane reply line (observation incl. full STATE) [TARGET].
const MaxLaneReply = 4 << 20

// laneClient is a minimal agent_lane/v1 client run inside the capsule (python3 stdlib).
// It refuses (exit 5) a reply line longer than MaxLaneReply bytes.
const laneClient = `import json,os,socket,sys
s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);b=b""
req={"nonce":os.urandom(16).hex(),"seq":1,"verb":"observe","body":{}}
s.sendall(json.dumps(req,separators=(",",":")).encode()+b"\n")
while b"\n" not in b:
  c=s.recv(1<<20)
  if not c or len(b)+len(c)>4194304+1: sys.exit(5)
  b+=c
l=b.split(b"\n",1)[0]
if len(l)>4194304: sys.exit(5)
sys.stdout.write(l.decode())`

// Observe returns the agent-lane observation (tick, revision, state, state_sha256, elements)
// as the lane's own JSON bytes: never round-tripped through float64, so integers in
// observation.state survive and sha256(canonicalBytes(state)) stays recomputable downstream.
func (t Tool) Observe(ctx context.Context, c string) (any, error) {
	if _, e := checkContainer(c); e != nil {
		return nil, e
	}
	for try := 0; try < 10; try++ {
		out, e := t.R.Run(ctx, []string{"docker", "exec", "-u", "10001", c, "python3", "-I", "-c", laneClient, LaneSocket}, nil)
		if e != nil {
			return nil, fail(5, "lane observe: %v", e)
		}
		if len(out) > MaxLaneReply {
			return nil, fail(5, "lane reply exceeds %d bytes", MaxLaneReply)
		}
		var r struct {
			Verdict     string          `json:"verdict"`
			Code        string          `json:"code"`
			Observation json.RawMessage `json:"observation"`
		}
		if e = json.Unmarshal(out, &r); e != nil {
			return nil, fail(5, "lane reply JSON")
		}
		if r.Verdict == "ok" {
			if len(r.Observation) == 0 || r.Observation[0] != '{' {
				return nil, fail(5, "lane observation missing")
			}
			return r.Observation, nil
		}
		if r.Code != "TICK_WINDOW" {
			return nil, fail(5, "lane observe refused %v", r.Code)
		}
		t.sleep(100 * time.Millisecond)
	}
	return nil, fail(6, "lane observe: tick window never settled")
}

// Stop terminates Holdings, removes the capsule and verifies nothing is left.
func (t Tool) Stop(ctx context.Context, c string) error {
	if _, e := checkContainer(c); e != nil {
		return e
	}
	_, _ = t.R.Run(ctx, []string{"docker", "exec", c, "sh", "-c", "pkill -TERM -f /opt/holdings/holdings-h2l; sleep 1"}, nil)
	if _, e := t.R.Run(ctx, []string{"docker", "rm", "-f", c}, nil); e != nil {
		return fail(6, "docker rm %s: %v", c, e)
	}
	out, e := t.R.Run(ctx, []string{"docker", "ps", "-a", "--filter", "name=^" + c + "$", "--format", "{{.Names}}"}, nil)
	if e != nil || len(bytes.TrimSpace(out)) != 0 {
		return fail(6, "capsule %s still present", c)
	}
	return nil
}
