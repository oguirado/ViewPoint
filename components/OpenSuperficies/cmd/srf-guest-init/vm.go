package main

import (
	"bufio"
	"bytes"
	"context"
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
	"sync"
	"syscall"
	"time"
)

const (
	controlPort   = "opensuperficies.control"
	agentPort     = "opensuperficies.agent"
	viewPort      = "opensuperficies.view"
	bootLineLimit = 64 * 1024
)

// The sysfs and device roots are arguments so discovery never depends on udev.
func discoverPorts(sysRoot, devRoot string) (map[string]string, error) {
	ports := make(map[string]string)
	entries, err := os.ReadDir(sysRoot)
	if os.IsNotExist(err) {
		return ports, nil
	}
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		b, err := os.ReadFile(filepath.Join(sysRoot, e.Name(), "name"))
		if err != nil {
			return nil, err
		}
		name := strings.TrimSpace(string(b))
		switch name {
		case controlPort, agentPort, viewPort:
			if _, ok := ports[name]; ok {
				return nil, fmt.Errorf("AUSENTE — duplicate virtio port %s", name)
			}
			ports[name] = filepath.Join(devRoot, e.Name())
		}
	}
	return ports, nil
}
func requireVMPorts(ports map[string]string) error {
	for _, name := range []string{controlPort, agentPort, viewPort} {
		if ports[name] == "" {
			return fmt.Errorf("AUSENTE — missing virtio port %s", name)
		}
	}
	return nil
}

type tile struct {
	ID string `json:"id"`
	X  int    `json:"x"`
	Y  int    `json:"y"`
	W  int    `json:"w"`
	H  int    `json:"h"`
}

var tileID = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,128}$`)

// exactObject validates spelling, presence and uniqueness before typed decoding.
// Encoding/json alone accepts duplicate keys and case-folded struct fields.
func exactObject(raw []byte, keys ...string) bool {
	d := json.NewDecoder(bytes.NewReader(raw))
	token, err := d.Token()
	if err != nil || token != json.Delim('{') {
		return false
	}
	allowed := make(map[string]bool, len(keys))
	for _, key := range keys {
		allowed[key] = true
	}
	seen := make(map[string]bool, len(keys))
	for d.More() {
		token, err = d.Token()
		key, ok := token.(string)
		if err != nil || !ok || !allowed[key] || seen[key] {
			return false
		}
		seen[key] = true
		var value json.RawMessage
		if d.Decode(&value) != nil || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return false
		}
	}
	token, err = d.Token()
	return err == nil && token == json.Delim('}') && len(seen) == len(keys) && d.Decode(new(any)) == io.EOF
}

func parseBootEnv(line []byte) ([]string, error) {
	bad := errors.New("AUSENTE — invalid guest boot environment")
	if !exactObject(line, "SRF_PROGRAM", "SRF_WINDOWS", "SRF_TILES", "NO_AT_BRIDGE") {
		return nil, bad
	}
	var env map[string]string
	dec := json.NewDecoder(bytes.NewReader(line))
	if dec.Decode(&env) != nil || len(env) != 4 {
		return nil, bad
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return nil, bad
	}
	for k := range env {
		switch k {
		case "SRF_PROGRAM", "SRF_WINDOWS", "SRF_TILES", "NO_AT_BRIDGE":
		default:
			return nil, bad
		}
	}
	if env["SRF_PROGRAM"] != "notepad" && env["SRF_PROGRAM"] != "notepad-vm" {
		return nil, bad
	}
	if env["NO_AT_BRIDGE"] != "0" && env["NO_AT_BRIDGE"] != "1" {
		return nil, bad
	}
	n, err := strconv.Atoi(env["SRF_WINDOWS"])
	if err != nil || n < 1 || n > 4 {
		return nil, bad
	}
	var tiles []tile
	dec = json.NewDecoder(strings.NewReader(env["SRF_TILES"]))
	dec.DisallowUnknownFields()
	if dec.Decode(&tiles) != nil || len(tiles) != n || dec.Decode(&extra) != io.EOF {
		return nil, bad
	}
	// Match the program entrypoint's exact tile shape, including required zero-valued fields.
	var rawTiles []json.RawMessage
	if json.Unmarshal([]byte(env["SRF_TILES"]), &rawTiles) != nil {
		return nil, bad
	}
	for _, raw := range rawTiles {
		if !exactObject(raw, "id", "x", "y", "w", "h") {
			return nil, bad
		}
	}
	seen := make(map[string]bool)
	for i, a := range tiles {
		if !tileID.MatchString(a.ID) || seen[a.ID] || a.X < 0 || a.Y < 0 || a.W < 1 || a.H < 1 || a.X > 1280 || a.Y > 900 || a.W > 1280-a.X || a.H > 900-a.Y {
			return nil, bad
		}
		seen[a.ID] = true
		for _, b := range tiles[:i] {
			if a.X < b.X+b.W && b.X < a.X+a.W && a.Y < b.Y+b.H && b.Y < a.Y+a.H {
				return nil, bad
			}
		}
	}
	result := []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/profile", "DISPLAY=:100", "LANG=C.UTF-8", "XDG_RUNTIME_DIR=/run/newton-tenant", "XDG_CACHE_HOME=/profile/cache", "XDG_CONFIG_HOME=/profile/config", "DBUS_SESSION_BUS_ADDRESS=disabled:", "GTK_USE_PORTAL=0", "GIO_USE_VFS=local", "DBUS_SYSTEM_BUS_ADDRESS=disabled:"}
	for _, k := range []string{"SRF_PROGRAM", "SRF_WINDOWS", "SRF_TILES", "NO_AT_BRIDGE"} {
		result = append(result, k+"="+env[k])
	}
	return result, nil
}

type commandSpec struct {
	Path     string
	Args     []string
	Env      []string
	UID, GID uint32
	Stdout   io.Writer
	Stderr   io.Writer
}
type childProcess struct {
	pid  int
	done <-chan error
	stop func()
}
type bootOps struct {
	profile func() (string, error)
	mkdir   func(string, os.FileMode) error
	chown   func(string, int, int) error
	chmod   func(string, os.FileMode) error
	mount   func(string, string, string, string) error
	check   func([]string) error
	start   func(commandSpec) (childProcess, error)
}

func prepareGuest(o bootOps) error {
	profile, err := o.profile()
	if err != nil {
		return err
	}
	for _, m := range []struct{ path, data string }{
		{"/run/vqa/fb", "size=64m,mode=0700,uid=10001,gid=10001"},
		{"/run/newton-tenant", "size=16m,mode=0700,uid=10001,gid=10001"},
		{"/tmp", "size=64m,mode=1777"},
		{"/run/srfview", "size=16m,mode=0700,uid=10001,gid=10001"},
		{"/run/user/10001", "size=16m,mode=0700,uid=10001,gid=10001"},
	} {
		if err := o.mkdir(m.path, 0700); err != nil {
			return err
		}
		if err := o.mount("tmpfs", m.path, "tmpfs", m.data); err != nil {
			return err
		}
	}
	if err := o.mkdir("/profile", 0700); err != nil {
		return err
	}
	if err := o.check([]string{"/sbin/fsck", "-p", profile}); err != nil {
		return err
	}
	if err := o.mount(profile, "/profile", "ext4", ""); err != nil {
		return err
	}
	for _, p := range []string{"/profile", "/profile/exchange", "/profile/cache", "/profile/config"} {
		if err := o.mkdir(p, 0700); err != nil {
			return err
		}
		if err := o.chown(p, 10001, 10001); err != nil {
			return err
		}
		if err := o.chmod(p, 0700); err != nil {
			return err
		}
	}
	return nil
}

// lineRelay prefixes complete stdout lines only. Its bounded buffer cannot turn
// program output into control commands, including when a write splits a line.
type lineRelay struct {
	mu      sync.Mutex
	pending []byte
	w       io.Writer
}

func (r *lineRelay) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	original := len(p)
	for len(p) > 0 {
		i := bytes.IndexByte(p, '\n')
		n := len(p)
		if i >= 0 {
			n = i + 1
		}
		if len(r.pending)+n > bootLineLimit {
			return 0, errors.New("AUSENTE — guest stdout line too long")
		}
		r.pending = append(r.pending, p[:n]...)
		p = p[n:]
		if i >= 0 {
			line := append([]byte("SRF1 OUT "), r.pending...)
			if _, err := r.w.Write(line); err != nil {
				return 0, err
			}
			r.pending = nil
		}
	}
	return original, nil
}

// A stalled control peer cannot keep PID 1 alive indefinitely. Serialize ERR
// against stdout so it is a complete control line before child cleanup begins.
func (r *lineRelay) reportError(control io.Closer, reason string) {
	timer := time.AfterFunc(time.Second, func() { _ = control.Close() })
	defer timer.Stop()
	r.mu.Lock()
	defer r.mu.Unlock()
	_, _ = io.WriteString(r.w, "SRF1 ERR "+reason+"\n")
}

func runGuest(ctx context.Context, control io.ReadWriteCloser, ports map[string]string, o bootOps) error {
	defer control.Close()
	// Serial devices are opened nonblocking so acquisition cannot stall PID 1.
	// Scanner must see blocking reads; EAGAIN would otherwise end boot at once.
	if device, ok := control.(interface{ Fd() uintptr }); ok {
		if err := syscall.SetNonblock(int(device.Fd()), false); err != nil {
			return fmt.Errorf("AUSENTE — blocking control device: %w", err)
		}
	}
	if err := requireVMPorts(ports); err != nil {
		return err
	}
	type event struct {
		line string
		err  error
	}
	events := make(chan event)
	readerCtx, readerCancel := context.WithCancel(ctx)
	defer readerCancel()
	go func() {
		scanner := bufio.NewScanner(control)
		scanner.Buffer(make([]byte, 4096), bootLineLimit)
		for scanner.Scan() {
			select {
			case events <- event{line: scanner.Text()}:
			case <-readerCtx.Done():
				return
			}
		}
		err := scanner.Err()
		if err == nil {
			err = io.EOF
		}
		select {
		case events <- event{err: err}:
		case <-readerCtx.Done():
		}
	}()
	timer := time.NewTimer(30 * time.Second)
	defer timer.Stop()
	var env []string
	select {
	case e := <-events:
		if e.err != nil {
			return fmt.Errorf("AUSENTE — boot control unavailable: %w", e.err)
		}
		var err error
		env, err = parseBootEnv([]byte(e.line))
		if err != nil {
			return err
		}
	case <-timer.C:
		return errors.New("AUSENTE — boot environment timeout")
	case <-ctx.Done():
		return ctx.Err()
	}
	if err := prepareGuest(o); err != nil {
		return fmt.Errorf("AUSENTE — guest filesystems: %w", err)
	}
	var children []childProcess
	defer func() {
		control.Close()
		for i := len(children) - 1; i >= 0; i-- {
			children[i].stop()
		}
	}()
	relay := &lineRelay{w: control}
	stderr := &programStderr{}
	specs := []commandSpec{
		{Path: "/usr/local/bin/srf-guest-agent", Args: []string{"--port", ports[agentPort], "--view-port", ports[viewPort]}},
		{Path: "/usr/local/bin/identity-entrypoint.sh", Stdout: relay, Stderr: stderr},
	}
	for _, s := range specs {
		s.Env = env
		s.UID = 10001
		s.GID = 10001
		child, err := o.start(s)
		if err != nil {
			if s.Stderr != nil {
				relay.reportError(control, "program start failed")
			}
			return fmt.Errorf("AUSENTE — guest child start: %w", err)
		}
		children = append(children, child)
	}
	// exec.Cmd owns direct children; only adopted children are reaped here.
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case e := <-events:
			if e.err != nil {
				return fmt.Errorf("AUSENTE — control peer lost: %w", e.err)
			}
			if e.line == "SRF1 stop" {
				return nil
			}
			return errors.New("AUSENTE — invalid control command")
		case <-children[0].done:
			relay.reportError(control, "agent exited")
			return errors.New("AUSENTE — agent exited")
		case <-children[1].done:
			relay.reportError(control, stderr.reason())
			return errors.New("AUSENTE — program exited")
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			reapAdopted(children)
		}
	}
}
func reapAdopted(children []childProcess) {
	// Go may spawn from any OS thread; each thread has its own children list.
	paths, _ := filepath.Glob("/proc/self/task/*/children")
	seen := make(map[int]bool)
	for _, path := range paths {
		b, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		for _, word := range strings.Fields(string(b)) {
			pid, err := strconv.Atoi(word)
			if err != nil || seen[pid] {
				continue
			}
			seen[pid] = true
			managed := false
			for _, c := range children {
				if c.pid == pid {
					managed = true
					break
				}
			}
			if !managed {
				var ws syscall.WaitStatus
				_, _ = syscall.Wait4(pid, &ws, syscall.WNOHANG, nil)
			}
		}
	}
}

func realBootOps() bootOps {
	return bootOps{
		profile: func() (string, error) { return discoverProfile("/sys/class/block", "/dev") },
		mkdir: func(p string, m os.FileMode) error {
			return makeGuestDirectory(p, m, directoryOps{mkdirAll: os.MkdirAll, mkdir: os.Mkdir, lstat: os.Lstat, chmod: os.Chmod})
		},
		chown: os.Chown, chmod: os.Chmod,
		mount: func(src, dst, kind, data string) error {
			return syscall.Mount(src, dst, kind, syscall.MS_NOSUID|syscall.MS_NODEV, data)
		},
		check: func(argv []string) error {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			err := exec.CommandContext(ctx, argv[0], argv[1:]...).Run()
			var ex *exec.ExitError
			// fsck status 1 means repairs completed successfully, not an init exit code.
			if errors.As(err, &ex) && ex.ExitCode() == 1 {
				return nil
			}
			return err
		},
		start: func(s commandSpec) (childProcess, error) {
			cmd := exec.Command(s.Path, s.Args...)
			cmd.Env = s.Env
			cmd.Stdout = s.Stdout
			cmd.Stderr = s.Stderr
			if cmd.Stderr == nil {
				cmd.Stderr = os.Stderr
			}
			cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Credential: &syscall.Credential{Uid: s.UID, Gid: s.GID, Groups: []uint32{}}}
			if err := cmd.Start(); err != nil {
				return childProcess{}, err
			}
			done := make(chan error, 1)
			go func() { done <- cmd.Wait(); close(done) }()
			stop := func() {
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
				select {
				case <-done:
				case <-time.After(3 * time.Second):
				}
				// Descendants may still own the process group after the leader exited.
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				select {
				case <-done:
				case <-time.After(time.Second):
				}
			}
			return childProcess{pid: cmd.Process.Pid, done: done, stop: stop}, nil
		},
	}
}
