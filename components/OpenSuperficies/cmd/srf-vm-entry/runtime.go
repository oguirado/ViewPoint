package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"opensuperficies.local/srf/src/vmsurface/launch"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// relayControl withholds both readiness lines until both have arrived. Each line
// is then emitted by one Write, in the surface's required order.
func relayControl(r io.Reader, w io.Writer) error {
	scan := bufio.NewScanner(r)
	scan.Buffer(make([]byte, 4096), 65536)
	xpra := false
	ready := ""
	emitted := false
	for scan.Scan() {
		s := scan.Text()
		if strings.HasPrefix(s, "SRF1 ERR ") {
			reason := strings.TrimPrefix(s, "SRF1 ERR ")
			line := "REFUSED: guest " + reason + "\n"
			n, err := w.Write([]byte(line))
			if err != nil {
				return err
			}
			if n != len(line) {
				return io.ErrShortWrite
			}
			return refused(4, "guest "+reason)
		}
		if s == "SRF1 OUT xpra layer up" {
			xpra = true
		}
		if strings.HasPrefix(s, "SRF1 OUT PROGRAM_READY ") {
			ready = strings.TrimPrefix(s, "SRF1 OUT ")
		}
		if xpra && ready != "" && !emitted {
			for _, line := range []string{"xpra layer up\n", ready + "\n"} {
				n, e := w.Write([]byte(line))
				if e != nil {
					return e
				}
				if n != len(line) {
					return io.ErrShortWrite
				}
			}
			emitted = true
		}
	}
	return scan.Err()
}

func verifyGuest(dir string) error {
	f, e := os.Open(filepath.Join(dir, "MANIFEST.sha256"))
	if e != nil {
		return refused(3, "guest manifest missing")
	}
	defer f.Close()
	// Read one byte past the limit so a valid prefix cannot hide trailing data.
	const manifestLimit = 4096
	raw, e := io.ReadAll(io.LimitReader(f, manifestLimit+1))
	if e != nil || len(raw) > manifestLimit {
		return refused(3, "guest manifest oversized or unreadable")
	}
	scan := bufio.NewScanner(strings.NewReader(string(raw)))
	seen := map[string]bool{}
	for scan.Scan() {
		line := strings.Fields(scan.Text())
		if len(line) != 2 {
			return refused(3, "invalid guest manifest")
		}
		name := strings.TrimPrefix(line[1], "*")
		if (name != "Image" && name != "root.raw") || seen[name] || len(line[0]) != 64 {
			return refused(3, "invalid guest manifest")
		}
		want, e := hex.DecodeString(line[0])
		if e != nil {
			return refused(3, "invalid guest digest")
		}
		p := filepath.Join(dir, name)
		st, e := os.Lstat(p)
		if e != nil || !st.Mode().IsRegular() {
			return refused(3, "guest file missing")
		}
		data, e := os.Open(p)
		if e != nil {
			return refused(3, "guest file unreadable")
		}
		h := sha256.New()
		_, e = io.Copy(h, data)
		data.Close()
		if e != nil || !strings.EqualFold(hex.EncodeToString(h.Sum(nil)), hex.EncodeToString(want)) {
			return refused(3, "guest digest mismatch")
		}
		seen[name] = true
	}
	if scan.Err() != nil || len(seen) != 2 {
		return refused(3, "incomplete guest manifest")
	}
	return nil
}

type commandRunner func(context.Context, string, ...string) error

func direct(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/home/worker"}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	return cmd.Run()
}

// verifyProfile accepts only the labelled bare ext4 layout. A guest-writable
// disk must not provide a competing root PARTUUID through a partition table.
func verifyProfile(path string) error {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return refused(6, "profile header unavailable")
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		return refused(6, "invalid profile disk")
	}
	var header [4104]byte
	if _, err = io.ReadFull(f, header[:]); err != nil {
		return refused(6, "profile header unreadable")
	}
	if string(header[512:520]) == "EFI PART" || string(header[4096:4104]) == "EFI PART" {
		return refused(6, "profile partition table forbidden")
	}
	if header[510] == 0x55 && header[511] == 0xaa {
		for entry := 0; entry < 4; entry++ {
			if header[446+entry*16+4] != 0 {
				return refused(6, "profile partition table forbidden")
			}
		}
	}
	var label [16]byte
	copy(label[:], "srf-profile")
	if header[1080] != 0x53 || header[1081] != 0xef || !bytes.Equal(header[1144:1160], label[:]) {
		return refused(6, "profile must be bare ext4 labelled srf-profile")
	}
	return nil
}

// prepareProfile serializes first creation, never follows disk symlinks, and
// publishes the journaled filesystem only after mke2fs and fsync succeed.
func prepareProfile(ctx context.Context, path string, mib int, run commandRunner) error {
	lock, e := os.OpenFile(filepath.Join(filepath.Dir(path), ".disk.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return refused(3, "profile lock unavailable")
	}
	defer lock.Close()
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		return refused(3, "profile disk busy")
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	size := int64(mib) * 1024 * 1024
	st, e := os.Lstat(path)
	if os.IsNotExist(e) {
		f, e := os.CreateTemp(filepath.Dir(path), ".disk.ext4-*")
		if e != nil {
			return refused(3, "profile creation unavailable")
		}
		tmp := f.Name()
		defer os.Remove(tmp)
		if e = f.Truncate(size); e != nil {
			f.Close()
			return refused(3, "profile allocation failed")
		}
		f.Close()
		if e = run(ctx, "mke2fs", "-q", "-t", "ext4", "-F", "-L", "srf-profile", "-O", "has_journal", "-E", "lazy_itable_init=0,lazy_journal_init=0", tmp); e != nil {
			return refused(3, "profile format failed")
		}
		if e = verifyProfile(tmp); e != nil {
			return e
		}
		f, e = os.OpenFile(tmp, os.O_RDWR|syscall.O_NOFOLLOW, 0)
		if e != nil {
			return refused(3, "profile sync unavailable")
		}
		e = f.Sync()
		f.Close()
		if e != nil {
			return refused(3, "profile sync failed")
		}
		if e = os.Rename(tmp, path); e != nil {
			return refused(3, "profile publication failed")
		}
		d, e := os.Open(filepath.Dir(path))
		if e != nil {
			return refused(3, "profile directory unavailable")
		}
		e = d.Sync()
		d.Close()
		if e != nil {
			return refused(3, "profile directory sync failed")
		}
	} else if e != nil || !st.Mode().IsRegular() || st.Size() != size {
		return refused(3, "invalid profile disk")
	}
	if e = verifyProfile(path); e != nil {
		return e
	}
	if e = run(ctx, "e2fsck", "-p", path); e != nil {
		if x, ok := e.(*exec.ExitError); !ok || x.ExitCode() != 1 {
			return refused(3, "profile fsck failed")
		}
	}
	return verifyProfile(path)
}

func dialPort(ctx context.Context, path string) (net.Conn, error) {
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		c, e := (&net.Dialer{Timeout: time.Second}).DialContext(ctx, "unix", path)
		if e == nil {
			return c, nil
		}
		select {
		case <-ctx.Done():
			return nil, refused(6, "VM port unavailable")
		case <-ticker.C:
		}
	}
}
func listenSocket(path string) (net.Listener, error) {
	if st, e := os.Lstat(path); e == nil {
		if st.Mode()&os.ModeSocket == 0 {
			return nil, refused(3, "socket path occupied")
		}
		if e = os.Remove(path); e != nil {
			return nil, e
		}
	} else if !os.IsNotExist(e) {
		return nil, e
	}
	l, e := net.Listen("unix", path)
	if e == nil {
		e = os.Chmod(path, 0600)
		if e != nil {
			l.Close()
		}
	}
	return l, e
}
func serve(ctx context.Context, l net.Listener, h *hub, view bool, wg *sync.WaitGroup) {
	defer wg.Done()
	limit := 128
	if view {
		limit = 32
	}
	slots := make(chan struct{}, limit)
	var handlers sync.WaitGroup
	defer handlers.Wait()
	for {
		c, e := l.Accept()
		if e != nil {
			return
		}
		select {
		case slots <- struct{}{}:
			handlers.Add(1)
			go func() {
				defer handlers.Done()
				defer func() { <-slots }()
				if view {
					relayView(ctx, c, h)
				} else {
					relayOne(ctx, c, h)
				}
			}()
		default:
			c.Close()
		}
	}
}
func boot(ctx context.Context, env map[string]string, out io.Writer) error {
	c, e := settings(env)
	if e != nil {
		return e
	}
	if e = verifyGuest("/usr/local/share/srf-vm"); e != nil {
		return e
	}
	lease, e := os.OpenFile(filepath.Join(filepath.Dir(c.Profile), ".vm.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return refused(3, "profile lease unavailable")
	}
	defer lease.Close()
	if e = syscall.Flock(int(lease.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		return refused(3, "profile already in use")
	}
	defer syscall.Flock(int(lease.Fd()), syscall.LOCK_UN)
	prep, cancel := context.WithTimeout(ctx, 2*time.Minute)
	e = prepareProfile(prep, c.Profile, c.DiskMiB, direct)
	cancel()
	if e != nil {
		return e
	}
	args, e := launch.Args(c.Config)
	if e != nil {
		return e
	}
	q := exec.Command("qemu-system-aarch64", args...)
	q.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/home/worker"}
	q.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	if e = q.Start(); e != nil {
		return refused(3, "QEMU launch unavailable")
	}
	exited := make(chan struct{})
	go func() { _ = q.Wait(); close(exited) }()
	defer func() {
		_ = syscall.Kill(-q.Process.Pid, syscall.SIGKILL)
		select {
		case <-exited:
		case <-time.After(5 * time.Second):
		}
	}()
	startup, stopStartup := context.WithTimeout(ctx, 60*time.Second)
	defer stopStartup()
	control, e := dialPort(startup, filepath.Join(c.RuntimeDir, "control.sock"))
	if e != nil {
		return e
	}
	defer control.Close()
	ac, e := dialPort(startup, filepath.Join(c.RuntimeDir, "agent.sock"))
	if e != nil {
		return e
	}
	ah := newHub(ac, 128)
	defer ah.close()
	vc, e := dialPort(startup, filepath.Join(c.RuntimeDir, "view.sock"))
	if e != nil {
		return e
	}
	vh := newHub(vc, 32)
	defer vh.close()
	al, e := listenSocket(filepath.Join(c.RuntimeDir, "vm-agent.sock"))
	if e != nil {
		return refused(3, "agent socket unavailable")
	}
	defer al.Close()
	vl, e := listenSocket("/srfview/xpra.sock")
	if e != nil {
		return refused(3, "view socket unavailable")
	}
	defer vl.Close()
	service, stop := context.WithCancel(ctx)
	var wg sync.WaitGroup
	wg.Add(2)
	go serve(service, al, ah, false, &wg)
	go serve(service, vl, vh, true, &wg)
	defer func() {
		stop()
		al.Close()
		vl.Close()
		ah.close()
		vh.close()
		wg.Wait()
	}()
	_ = control.SetWriteDeadline(time.Now().Add(5 * time.Second))
	if e = json.NewEncoder(control).Encode(c.Boot); e != nil {
		return refused(6, "boot environment relay failed")
	}
	return superviseGuest(ctx, control, out, exited, ah.done, vh.done)
}

// Returning from supervision runs boot's deferred QEMU kill/reap and relay
// cleanup. A guest refusal must retain its status rather than become an EOF.
func superviseGuest(ctx context.Context, control net.Conn, out io.Writer, exited, agentDone, viewDone <-chan struct{}) (result error) {
	var controlErr error
	controlFinished := make(chan struct{})
	go func() {
		controlErr = relayControl(control, out)
		close(controlFinished)
	}()
	defer func() {
		// Guest poweroff may close another port before its final ERR has
		// been read. Drain control briefly so that refusal wins that race.
		if err := control.SetReadDeadline(time.Now().Add(100 * time.Millisecond)); err != nil {
			control.Close()
		}
		<-controlFinished
		control.Close()
		if refusal, ok := controlErr.(*proto.Error); ok && refusal.Code == 4 {
			result = refusal
		}
	}()
	select {
	case <-ctx.Done():
		_ = control.SetWriteDeadline(time.Now().Add(time.Second))
		_, _ = fmt.Fprintln(control, "SRF1 stop")
		select {
		case <-exited:
		case <-time.After(10 * time.Second):
		}
		return nil
	case <-exited:
		return refused(6, "QEMU exited")
	case <-agentDone:
		return refused(6, "agent peer disconnected")
	case <-viewDone:
		return refused(6, "view peer disconnected")
	case <-controlFinished:
		if refusal, ok := controlErr.(*proto.Error); ok && refusal.Code == 4 {
			return refusal
		}
		return refused(6, "control peer disconnected")
	}
}
