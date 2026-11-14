package main

import (
	"fmt"
	"io"
	"os"
	"syscall"
	"time"
)

// legacyOps keeps the pre-VM PID 1 path testable without mounting or rebooting
// the host. The operation sequence is pinned to dfb68a6 by a golden trace.
type legacyOps struct {
	mount    func(string, string, string, uintptr, string)
	symlink  func(string, string)
	mkdir    func(string, os.FileMode)
	stdout   io.Writer
	notify   func(string)
	reap     func()
	sync     func()
	poweroff func()
}

func realLegacyOps() legacyOps {
	return legacyOps{
		mount:    mountVirtual,
		symlink:  func(src, dst string) { _ = os.Symlink(src, dst) },
		mkdir:    func(path string, mode os.FileMode) { _ = os.MkdirAll(path, mode) },
		stdout:   os.Stdout,
		notify:   notifyControl,
		reap:     reapZombies,
		sync:     syscall.Sync,
		poweroff: func() { _ = syscall.Reboot(syscall.LINUX_REBOOT_CMD_POWER_OFF) },
	}
}

func setupLegacyFilesystems(o legacyOps) {
	o.mount("proc", "/proc", "proc", 0, "")
	o.mount("sysfs", "/sys", "sysfs", 0, "")
	o.mount("devtmpfs", "/dev", "devtmpfs", 0, "")
	o.mount("devpts", "/dev/pts", "devpts", 0, "newinstance,ptmxmode=0666,mode=0620")
	o.mount("tmpfs", "/dev/shm", "tmpfs", 0, "mode=1777")
	o.mount("tmpfs", "/run", "tmpfs", 0, "mode=0755")
	o.mount("tmpfs", "/tmp", "tmpfs", 0, "mode=1777")
	o.symlink("pts/ptmx", "/dev/ptmx")
	o.mkdir("/run/newton-tenant", 0770)
	o.mkdir("/run/user/10001", 0700)
}

func announceLegacy(o legacyOps) {
	fmt.Fprintln(o.stdout, "[srf-guest-init] MicroVM guest PID 1 initialized")
	o.notify("SRF1 PID1")
}

func runLegacy(signals <-chan os.Signal, ticks <-chan time.Time, o legacyOps) {
	for {
		select {
		case sig := <-signals:
			switch sig {
			case syscall.SIGCHLD:
				o.reap()
			case syscall.SIGTERM, syscall.SIGINT:
				fmt.Fprintf(o.stdout, "[srf-guest-init] received shutdown signal: %v\n", sig)
				o.notify("SRF1 SHUTDOWN_BEGIN")
				o.sync()
				o.notify("SRF1 POWER_OFF")
				o.poweroff()
				return
			case syscall.SIGHUP:
				// ignore or reload
			}
		case <-ticks:
			o.reap()
		}
	}
}
