package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func mountVirtual(source, target, fstype string, flags uintptr, data string) {
	if err := os.MkdirAll(target, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "[srf-guest-init] mkdir %s error: %v\n", target, err)
	}
	if err := syscall.Mount(source, target, fstype, flags, data); err != nil {
		// devtmpfs may already be mounted by kernel CONFIG_DEVTMPFS_MOUNT
		if err != syscall.EBUSY {
			fmt.Fprintf(os.Stderr, "[srf-guest-init] mount %s on %s error: %v\n", source, target, err)
		}
	}
}

func reapZombies() {
	for {
		var ws syscall.WaitStatus
		pid, err := syscall.Wait4(-1, &ws, syscall.WNOHANG, nil)
		if err != nil || pid <= 0 {
			break
		}
	}
}

func notifyControl(msg string) {
	candidates := []string{
		"/dev/virtio-ports/opensuperficies.control",
		"/dev/vport0p1",
	}
	for _, p := range candidates {
		if f, err := os.OpenFile(p, os.O_WRONLY, 0); err == nil {
			f.WriteString(msg + "\n")
			f.Close()
			return
		}
	}
}

func main() {
	syscall.Umask(0022)

	ops := realLegacyOps()
	setupLegacyFilesystems(ops)

	ports, discoveryErr := discoverPorts("/sys/class/virtio-ports", "/dev")
	if discoveryErr != nil {
		fmt.Fprintf(os.Stderr, "AUSENTE — virtio discovery: %v\n", discoveryErr)
		os.Exit(3)
	}
	if ports[agentPort] != "" {
		if err := requireVMPorts(ports); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(3)
		}
		for _, name := range []string{agentPort, viewPort} {
			if err := os.Chown(ports[name], 10001, 10001); err != nil {
				fmt.Fprintf(os.Stderr, "AUSENTE — guest port permissions: %v\n", err)
				os.Exit(3)
			}
			if err := os.Chmod(ports[name], 0600); err != nil {
				fmt.Fprintf(os.Stderr, "AUSENTE — guest port permissions: %v\n", err)
				os.Exit(3)
			}
		}
		control, err := os.OpenFile(ports[controlPort], os.O_RDWR|syscall.O_NONBLOCK, 0)
		if err != nil {
			fmt.Fprintf(os.Stderr, "AUSENTE — control device unavailable: %v\n", err)
			os.Exit(3)
		}
		ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
		err = runGuest(ctx, control, ports, realBootOps())
		cancel()
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
		}
		syscall.Sync()
		_ = syscall.Reboot(syscall.LINUX_REBOOT_CMD_POWER_OFF)
		if err != nil {
			os.Exit(3)
		}
		return
	}
	// The legacy path is selected only when the agent port is absent.
	announceLegacy(ops)
	sigCh := make(chan os.Signal, 64)
	signal.Notify(sigCh, syscall.SIGCHLD, syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	runLegacy(sigCh, ticker.C, ops)
}
