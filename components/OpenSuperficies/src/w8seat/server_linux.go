//go:build linux

package w8seat

import (
	"context"
	"net"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// Serve accepts only kernel-authenticated admin peers. No caller-supplied UID,
// supplementary-group list, executable, path, environment or launch flags exist.
func (s *Seat) Serve(ctx context.Context, socket string) error {
	if socket != SocketPath {
		return refusal(2, "contract", "seat endpoint is fixed")
	}
	admin, e := user.LookupGroup("admin")
	if e != nil {
		return refusal(4, "unavailable", "admin group unavailable")
	}
	gid, e := strconv.Atoi(admin.Gid)
	if e != nil {
		return e
	}
	st, e := os.Lstat(filepath.Dir(socket))
	if e != nil || !st.IsDir() || st.Mode().Perm() != 0750 {
		return refusal(4, "unavailable", "seat runtime directory unavailable")
	}
	meta, ok := st.Sys().(*syscall.Stat_t)
	if !ok || int(meta.Uid) != os.Geteuid() || int(meta.Gid) != gid {
		return refusal(4, "unavailable", "seat runtime directory owner")
	}
	if old, e := os.Lstat(socket); e == nil {
		v, ok := old.Sys().(*syscall.Stat_t)
		if !ok || old.Mode()&os.ModeSocket == 0 || int(v.Uid) != os.Geteuid() {
			return refusal(5, "containment", "unsafe stale endpoint")
		}
		if e = os.Remove(socket); e != nil {
			return e
		}
	} else if !os.IsNotExist(e) {
		return e
	}
	listener, e := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if e != nil {
		return e
	}
	defer listener.Close()
	defer os.Remove(socket)
	if e = os.Chown(socket, os.Geteuid(), gid); e != nil {
		return e
	}
	if e = os.Chmod(socket, 0660); e != nil {
		return e
	}
	var mu sync.Mutex
	conns := map[*net.UnixConn]bool{}
	var wg sync.WaitGroup
	done := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			listener.Close()
			mu.Lock()
			for c := range conns {
				c.Close()
			}
			mu.Unlock()
		case <-done:
		}
	}()
	defer close(done)
	slots := make(chan struct{}, 64)
	for {
		conn, e := listener.AcceptUnix()
		if e != nil {
			wg.Wait()
			if ctx.Err() != nil {
				return nil
			}
			return e
		}
		select {
		case slots <- struct{}{}:
		default:
			_ = WriteFrame(conn, Response{ExitCode: 4, Reason: "unavailable", BridgeResult: nil})
			conn.Close()
			continue
		}
		mu.Lock()
		conns[conn] = true
		mu.Unlock()
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-slots; mu.Lock(); delete(conns, conn); mu.Unlock(); conn.Close() }()
			_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
			creds, e := peerCredentials(conn)
			if e == nil {
				e = AuthorizePeer(creds.Uid, creds.Gid, admin.Gid, systemGroups)
			}
			if e != nil {
				_ = WriteFrame(conn, Response{ExitCode: 7, Reason: "operator_capability", BridgeResult: nil})
				return
			}
			var req Request
			if e = ReadFrame(conn, &req); e != nil {
				_ = WriteFrame(conn, Response{ExitCode: ExitCode(e), Reason: reasonOf(e), BridgeResult: nil})
				return
			}
			var response Response
			if req.Verb == "submit" {
				response, e = s.Submit(creds.Uid, req, conn)
			} else {
				response, e = s.Request(ctx, creds.Uid, req)
			}
			if e != nil {
				response = Response{ExitCode: ExitCode(e), Reason: reasonOf(e), BridgeResult: nil}
			}
			_ = WriteFrame(conn, response)
		}()
	}
}
func peerCredentials(c *net.UnixConn) (*syscall.Ucred, error) {
	raw, e := c.SyscallConn()
	if e != nil {
		return nil, e
	}
	var cred *syscall.Ucred
	var sockErr error
	e = raw.Control(func(fd uintptr) {
		cred, sockErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	})
	if e != nil {
		return nil, e
	}
	return cred, sockErr
}
func reasonOf(e error) string { return workerReason(e) }
