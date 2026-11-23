package capsule

// [TARGET, unit decision] Host-xpra uses one unreaped Xvfb group anchor; xpra
// and its shell-free child inherit that group. All browser state lives in RAM.
// [TARGET, unit decision] Startup requires a visible normal window whose XID
// and witness remain unchanged across consecutive native inventories.
import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type hostXpraLog struct {
	mu   sync.Mutex
	tail []byte
}

func (l *hostXpraLog) Write(data []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := len(data)
	if n >= 8192 {
		l.tail = append(l.tail[:0], data[n-8192:]...)
	} else {
		l.tail = append(l.tail, data...)
		if len(l.tail) > 8192 {
			l.tail = append(l.tail[:0], l.tail[len(l.tail)-8192:]...)
		}
	}
	return n, nil
}
func (l *hostXpraLog) String() string { l.mu.Lock(); defer l.mu.Unlock(); return string(l.tail) }

type hostXpraProcess struct {
	// Optional probe seam; cleanup always verifies the real process witness.
	probe                                 func(int) (string, int, string, error)
	ownsSocket                            func(int, string) bool
	log                                   *hostXpraLog
	anchor, server                        *exec.Cmd
	start, root, view, display, authority string
	searchPath                            string
	port                                  int
	authorityInfo                         os.FileInfo
	target                                hostWindowTarget
	frame                                 hostFrameWitness // Protected by the manager lock.
}

type hostWindowTarget struct {
	xid     uint32
	witness string
	pid     int
	start   string
}

func (p *hostXpraProcess) processStart(pid int) string {
	if p == nil || p.anchor == nil || p.anchor.Process == nil || pid <= 0 {
		return ""
	}
	start, group, state, err := processWitness(pid)
	if err != nil || group != p.anchor.Process.Pid || state == "Z" || state == "X" {
		return ""
	}
	return start
}

func (p *hostXpraProcess) displayOwned(display, authority string) bool {
	if p == nil || p.anchor == nil || p.anchor.Process == nil || display != p.display || authority != p.authority ||
		p.start == "" || p.authorityInfo == nil || filepath.Dir(p.root) != "/dev/shm" ||
		!strings.HasPrefix(filepath.Base(p.root), "mv-host-xpra-") || authority != filepath.Join(p.root, "Xauthority") {
		return false
	}
	n, err := strconv.Atoi(strings.TrimPrefix(display, ":"))
	if err != nil || n < 10000 || n > 59999 || display != ":"+strconv.Itoa(n) {
		return false
	}
	pid := p.anchor.Process.Pid
	if p.processStart(pid) != p.start {
		return false
	}
	dir, err := os.Lstat(p.root)
	if err != nil || dir.Mode() != os.ModeDir|0700 || dir.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) {
		return false
	}
	info, err := os.Lstat(authority)
	if err != nil || info.Mode() != 0600 || !os.SameFile(info, p.authorityInfo) {
		return false
	}
	ownsSocket := p.ownsSocket
	if ownsSocket == nil {
		ownsSocket = processOwnsDisplaySocket
	}
	return ownsSocket(pid, display)
}

// Check socket ownership through procfs before making any connection. A live
// process on a different socket, or a foreign listener at this address, proves
// no display authority.
func processOwnsDisplaySocket(pid int, display string) bool {
	paths := hostDisplaySockets(display)
	fds := fmt.Sprintf("/proc/%d/fd", pid)
	entries, err := os.ReadDir(fds)
	if err != nil {
		return false
	}
	sockets := make(map[string]bool)
	for _, entry := range entries {
		link, err := os.Readlink(filepath.Join(fds, entry.Name()))
		if err == nil && strings.HasPrefix(link, "socket:[") {
			sockets[strings.TrimSuffix(strings.TrimPrefix(link, "socket:["), "]")] = true
		}
	}
	table, err := os.Open("/proc/net/unix")
	if err != nil {
		return false
	}
	defer table.Close()
	scanner := bufio.NewScanner(table)
	seen := false
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 8 || (fields[7] != paths[0] && fields[7] != paths[1]) {
			continue
		}
		if fields[3] == "00010000" && fields[4] == "0001" && fields[5] == "01" {
			if !sockets[fields[6]] {
				return false
			}
			seen = true
		}
	}
	return seen && scanner.Err() == nil
}

func processWitness(pid int) (string, int, string, error) {
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return "", 0, "", err
	}
	end := strings.LastIndexByte(string(raw), ')')
	if end < 0 {
		return "", 0, "", errors.New("invalid process witness")
	}
	f := strings.Fields(string(raw)[end+1:])
	if len(f) < 20 {
		return "", 0, "", errors.New("short process witness")
	}
	group, err := strconv.Atoi(f[2])
	return f[19], group, f[0], err
}
func liveProcessGroup(group int) bool {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return true
	}
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		_, g, state, err := processWitness(pid)
		if err == nil && g == group && state != "Z" && state != "X" {
			return true
		}
	}
	return false
}
func (p *hostXpraProcess) stop() error {
	if p == nil || p.anchor == nil || p.anchor.Process == nil {
		return fault("host_program_unavailable", "recorded process group absent")
	}
	pid := p.anchor.Process.Pid
	start, group, _, err := processWitness(pid)
	// No asynchronous Wait: this unreaped child reserves the PID through killpg.
	if err != nil || start != p.start || group != pid {
		return fault("host_program_unavailable", "stale process witness: no signal sent")
	}
	if err = syscall.Kill(-pid, syscall.SIGTERM); err != nil && err != syscall.ESRCH {
		return err
	}
	deadline := time.Now().Add(2 * time.Second)
	for liveProcessGroup(pid) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if liveProcessGroup(pid) {
		if err = syscall.Kill(-pid, syscall.SIGKILL); err != nil && err != syscall.ESRCH {
			return err
		}
	}
	if p.server != nil {
		_ = p.server.Wait()
	}
	_ = p.anchor.Wait()
	deadline = time.Now().Add(time.Second)
	for liveProcessGroup(pid) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if liveProcessGroup(pid) {
		return fault("host_program_unavailable", "recorded group removal unverified")
	}
	if err := os.RemoveAll(p.view); err != nil {
		return err
	}
	return os.RemoveAll(p.root)
}

// xpra 3.1 uses shlex.split followed by Popen(shell=False), not a shell.
func xpraChild(path, root string) string {
	argv := []string{path}
	name := strings.ToLower(filepath.Base(path))
	if strings.Contains(name, "chromium") || strings.Contains(name, "chrome") {
		argv = append(argv, "--user-data-dir="+filepath.Join(root, "browser"), "--no-first-run", "--no-default-browser-check")
	} else if strings.Contains(name, "firefox") {
		argv = append(argv, "--no-remote", "--profile", filepath.Join(root, "browser"))
	}
	for i, arg := range argv {
		argv[i] = "'" + strings.ReplaceAll(arg, "'", "'\"'\"'") + "'"
	}
	return strings.Join(argv, " ")
}

// appendAbstractXauth adds a wildcard entry for this private file's cookie. A client reaching
// Xvfb through the abstract socket (see hostDisplaySockets) has no display number to match; the
// file serves one private display, so no other server gains this cookie.
func appendAbstractXauth(path string, cookie []byte) error {
	const familyWild = 0xffff
	entry := binary.BigEndian.AppendUint16(nil, familyWild)
	for _, field := range [][]byte{nil, nil, []byte("MIT-MAGIC-COOKIE-1"), cookie} {
		entry = binary.BigEndian.AppendUint16(entry, uint16(len(field)))
		entry = append(entry, field...)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0600)
	if err != nil {
		return err
	}
	if _, err = file.Write(entry); err != nil {
		_ = file.Close()
		return err
	}
	return file.Close()
}

// hostDisplaySockets lists where Xvfb can listen. Under WSLg /tmp/.X11-unix is a read-only
// mount, so Xvfb serves only the abstract socket and the path never appears.
func hostDisplaySockets(display string) []string {
	path := filepath.Join("/tmp/.X11-unix", "X"+strings.TrimPrefix(display, ":"))
	return []string{path, "@" + path}
}

func waitHostSocket(ctx context.Context, paths []string, child *exec.Cmd, label string, log *hostXpraLog) error {
	for {
		// Inspect without Wait: the unreaped anchor must continue reserving its
		// process-group witness until stop has signalled and verified removal.
		if child != nil && child.Process != nil {
			_, _, state, err := processWitness(child.Process.Pid)
			if err != nil || state == "Z" || state == "X" {
				return fault("host_program_unavailable", label+" exited before socket readiness: "+log.String())
			}
		}
		for _, path := range paths {
			c, err := net.DialTimeout("unix", path, 100*time.Millisecond)
			if err == nil {
				c.Close()
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return fault("host_program_unavailable", "private display/socket readiness timed out")
		case <-time.After(50 * time.Millisecond):
		}
	}
}
func (p *hostXpraProcess) environment(path string) []string {
	return []string{
		"PATH=" + path, "HOME=" + p.root, "LANG=C.UTF-8",
		"XDG_CONFIG_HOME=" + filepath.Join(p.root, "config"),
		"XDG_CACHE_HOME=" + filepath.Join(p.root, "cache"),
		"XDG_DATA_HOME=" + filepath.Join(p.root, "data"),
		"XDG_RUNTIME_DIR=" + filepath.Join(p.root, "runtime"),
		"XAUTHORITY=" + p.authority, "DISPLAY=" + p.display,
		"XPRA_DEFAULT_CONF_DIRS=" + filepath.Join(p.root, "config"),
		"XPRA_SYSTEM_CONF_DIRS=" + filepath.Join(p.root, "config"),
		"XPRA_USER_CONF_DIRS=" + filepath.Join(p.root, "config"),
		"GDK_BACKEND=x11", "QT_QPA_PLATFORM=xcb",
	}
}

func (p *hostXpraProcess) xvfbArgs() []string {
	return []string{p.display, "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-noreset", "+extension", "Composite", "-auth", p.authority}
}

func (p *hostXpraProcess) xpraArgs(program, role string) []string {
	return []string{"start-desktop", p.display, "--readonly=" + strconv.FormatBool(role == "agent"), "--use-display=yes", "--start=", "--daemon=no", "--bind=" + filepath.Join(p.view, "s", "xpra.sock"), "--socket-dir=" + filepath.Join(p.root, "runtime"), "--socket-dirs=" + filepath.Join(p.root, "runtime"), "--socket-permissions=600", "--bind-tcp=127.0.0.1:" + strconv.Itoa(p.port), "--html=on", "--mdns=no", "--notifications=no", "--pulseaudio=no", "--printing=no", "--webcam=no", "--dbus-proxy=no", "--dbus-launch=", "--clipboard=no", "--start-new-commands=no", "--exit-with-client=no", "--exit-with-children=yes", "--start-child=" + xpraChild(program, p.root)}
}

func startHostXpra(ctx context.Context, r *ProcessRunner, path, container, role string) (_ *hostXpraProcess, err error) {
	// A proven pre-spawn refusal or verified startup compensation leaves no
	// incumbent. Callers may release their in-flight reservation in that case.
	clean := true
	defer func() {
		if err != nil && clean {
			err = &preSpawnRefusal{err}
		}
	}()
	if os.Geteuid() == 0 {
		return nil, fault("host_program_unavailable", "private Xpra requires an unprivileged user")
	}
	if err = r.validateStateDir(); err != nil {
		return nil, err
	}
	tools := map[string]string{}
	for _, name := range []string{"xpra", "Xvfb", "xauth", "xdotool"} {
		tools[name], err = ResolveHostBinary(name, r.SearchPath())
		if err != nil {
			return nil, fault("host_program_unavailable", "host_program_unavailable: "+name+" missing; install xpra xvfb xauth xdotool (apt install xpra xvfb xauth xdotool)")
		}
	}
	if _, err = ResolveHostBinary(filepath.Join(r.root, "srf-host-window"), r.SearchPath()); err != nil {
		return nil, fault("host_program_unavailable", "host_program_unavailable: srf-host-window missing")
	}
	root, err := os.MkdirTemp("/dev/shm", "mv-host-xpra-")
	if err != nil {
		return nil, err
	}
	p := &hostXpraProcess{log: &hostXpraLog{}, root: root, view: filepath.Join(r.stateDir, "view", container), searchPath: r.SearchPath(), authority: filepath.Join(root, "Xauthority")}
	defer func() {
		if err != nil {
			if p.anchor != nil && p.start != "" {
				stopErr := p.stop()
				clean = stopErr == nil
				err = errors.Join(err, stopErr)
			} else {
				_ = os.RemoveAll(root)
				_ = os.RemoveAll(p.view)
			}
		}
	}()
	for _, dir := range []string{filepath.Join(root, "browser"), filepath.Join(root, "config"), filepath.Join(root, "cache"), filepath.Join(root, "data"), filepath.Join(root, "runtime"), filepath.Join(r.stateDir, "view"), p.view, filepath.Join(p.view, "s")} {
		if err = os.MkdirAll(dir, 0700); err != nil {
			return nil, err
		}
	}
	var entropy [20]byte
	if _, err = rand.Read(entropy[:]); err != nil {
		return nil, err
	}
	// No fixed display pool; Xvfb's atomic X lock refuses any random collision.
	p.display = ":" + strconv.Itoa(10000+int(binary.BigEndian.Uint32(entropy[:4])%50000))
	cookie := entropy[4:]
	env := p.environment(r.SearchPath())
	if err = os.WriteFile(p.authority, nil, 0600); err != nil {
		return nil, err
	}
	auth := exec.CommandContext(ctx, tools["xauth"], "-f", p.authority, "add", p.display, "MIT-MAGIC-COOKIE-1", hex.EncodeToString(cookie))
	auth.Env = env
	if err = auth.Run(); err != nil {
		return nil, fault("host_program_unavailable", "private Xauthority creation failed")
	}
	if err = appendAbstractXauth(p.authority, cookie); err != nil {
		return nil, fault("host_program_unavailable", "private Xauthority creation failed")
	}
	p.authorityInfo, err = os.Lstat(p.authority)
	if err != nil || p.authorityInfo.Mode() != 0600 {
		return nil, fault("host_program_unavailable", "private Xauthority file required")
	}
	p.anchor = exec.Command(tools["Xvfb"], p.xvfbArgs()...)
	p.anchor.Env = env
	p.anchor.Stderr = p.log
	p.anchor.WaitDelay = time.Second
	p.anchor.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err = p.anchor.Start(); err != nil {
		return nil, err
	}
	clean = false
	p.start, _, _, err = processWitness(p.anchor.Process.Pid)
	if err != nil {
		return nil, err
	}
	ready, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	for !p.displayOwned(p.display, p.authority) {
		if p.processStart(p.anchor.Process.Pid) != p.start {
			return nil, fault("host_program_unavailable", "Xvfb exited before proving private display ownership")
		}
		select {
		case <-ready.Done():
			return nil, fault("host_program_unavailable", "Xvfb did not prove private display ownership")
		case <-time.After(50 * time.Millisecond):
		}
	}
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		return nil, e
	}
	p.port = l.Addr().(*net.TCPAddr).Port
	l.Close()
	p.server = exec.Command(tools["xpra"], p.xpraArgs(path, role)...)
	p.server.Env = env
	p.server.Stdout = p.log
	p.server.Stderr = p.log
	p.server.WaitDelay = time.Second
	p.server.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pgid: p.anchor.Process.Pid}
	if err = p.server.Start(); err != nil {
		p.server = nil
		return nil, err
	}
	if err = waitHostSocket(ready, []string{filepath.Join(p.view, "s", "xpra.sock")}, p.server, "xpra", p.log); err != nil {
		return nil, err
	}
	return p, nil
}
func (m *Manager) createHostXpra(ctx context.Context, profile, id, version string, p Profile, path string, geom Geometry) ([]json.RawMessage, error) {
	r, ok := m.runner.(*ProcessRunner)
	if !ok {
		return nil, fault("configuration", "host-xpra requires process runner")
	}
	container := "srf-pg-hx-" + profile
	// Reserve capacity and the profile before leaving the manager lock. Native
	// readiness must not delay emergency control of an unrelated surface.
	m.quarantined[profile] = true
	m.mu.Unlock()
	locked, releaseReservation := false, false
	defer func() {
		if !locked {
			m.mu.Lock()
		}
		if releaseReservation {
			delete(m.quarantined, profile)
		}
	}()
	start := m.hostXpraStart
	if start == nil {
		start = startHostXpra
	}
	process, err := start(ctx, r, path, container, p.Role)
	if err != nil {
		var clean *preSpawnRefusal
		releaseReservation = errors.As(err, &clean)
		return nil, err
	}
	success := false
	defer func() {
		if !success {
			releaseReservation = process.stop() == nil
			if locked && !releaseReservation {
				m.quarantined[profile] = true
			}
		}
	}()
	probe := process.probe
	if probe == nil {
		probe = processWitness
	}
	var spec *HostWindowSpec
	var candidate hostWindowTarget
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) && ctx.Err() == nil {
		// The xpra child exits with its program. Keep its PID unreaped for
		// cleanup, but stop discovery as soon as that witnessed child exits.
		if process.server != nil && process.server.Process != nil {
			_, _, state, probeErr := probe(process.server.Process.Pid)
			// A failed probe is unknown; only a successful exit witness ends discovery.
			if probeErr == nil && (state == "Z" || state == "X") {
				return nil, fault("host_program_unavailable", "host_program_unavailable: program exited before publishing an X11 window")
			}
		}
		rows, listErr := m.listOwnedHostWindows(ctx, process)
		if listErr != nil && !process.displayOwned(process.display, process.authority) {
			return nil, listErr
		}
		var observed hostWindowTarget
		for _, w := range rows {
			observed = w.target(process)
			if observed.start == "" {
				continue
			}
			if observed == candidate {
				process.target = observed
				spec = &HostWindowSpec{XID: w.XID, Witness: w.Witness, Title: w.Title, Program: filepath.Base(path), Display: process.display, Xauthority: process.authority}
			}
			break
		}
		if spec != nil {
			break
		}
		candidate = observed
		select {
		case <-ctx.Done():
		case <-time.After(100 * time.Millisecond):
		}
	}
	if spec == nil {
		return nil, fault("host_program_unavailable", "host_program_unavailable: program did not publish an X11 window on its private display")
	}
	// [TARGET, unit decision] Fit only the witnessed app on its owned private
	// display. XConfigureWindow via xdotool needs no window manager.
	if err = fitHostXpraWindow(ctx, r, process, spec.XID); err != nil {
		return nil, err
	}
	m.mu.Lock()
	locked = true
	p.Display = process.display
	m.cfg.Profiles[profile] = p
	if _, err = m.attachOwnedHostWindow(profile, id, version, p, spec, geom, process); err != nil {
		return nil, err
	}
	b := m.bindings[id]
	b.Program = path
	b.Container = container
	b.Role = p.Role
	if p.Role == "human" {
		b.Surface.Control = "human"
	}
	m.bindings[id] = b
	if err = m.publishHumanBinding(b); err != nil {
		cleanupErr := m.detachHostWindowLocked(b)
		delete(m.bindings, id)
		return nil, errors.Join(err, cleanupErr)
	}
	success = true
	return []json.RawMessage{surfaceEvent(version, "created", b.Surface)}, nil
}

func fitHostXpraWindow(ctx context.Context, r *ProcessRunner, p *hostXpraProcess, xid uint32) error {
	if !p.displayOwned(p.display, p.authority) || p.target.xid != xid || !p.target.current(p) {
		return fault("host_window_unavailable", "owned window required before private layout")
	}
	tool, err := ResolveHostBinary("xdotool", r.SearchPath())
	if err != nil {
		return fault("host_program_unavailable", "host_program_unavailable: xdotool missing")
	}
	cmd := exec.CommandContext(ctx, tool, "windowsize", strconv.FormatUint(uint64(xid), 10), "1280", "900", "windowmove", strconv.FormatUint(uint64(xid), 10), "0", "0")
	cmd.Env = p.environment(r.SearchPath())
	if err = cmd.Run(); err != nil {
		return fault("host_program_unavailable", "host_program_unavailable: private window layout failed")
	}
	return nil
}
