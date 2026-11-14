// Package launch builds the offline, network-free VM capsule QEMU command.
package launch

import (
	"opensuperficies.local/srf/src/vmsurface/proto"
	"path/filepath"
	"regexp"
	"strconv"
)

type Config struct {
	CPUs, MemoryMiB     int
	RuntimeDir, Profile string
}

var safePath = regexp.MustCompile(`^/[A-Za-z0-9_./-]+$`)

// Args returns arguments only; the caller executes qemu-system-aarch64 directly.
func Args(c Config) ([]string, error) {
	if c.CPUs < 1 || c.CPUs > 5 || c.MemoryMiB < 512 || c.MemoryMiB > 7168 || !safePath.MatchString(c.RuntimeDir) || !safePath.MatchString(c.Profile) || filepath.Clean(c.RuntimeDir) != c.RuntimeDir || filepath.Clean(c.Profile) != c.Profile {
		return nil, &proto.Error{Code: 2, Detail: "AUSENTE — invalid VM launch configuration"}
	}
	// Retain profile-first ordering as defence in depth for reverse virtio-mmio
	// enumeration; PARTUUID selects the root independently of device names.
	a := []string{"-machine", "virt-8.2,accel=kvm,gic-version=host", "-cpu", "host,pmu=off", "-smp", strconv.Itoa(c.CPUs), "-m", strconv.Itoa(c.MemoryMiB), "-nodefaults", "-no-user-config", "-display", "none", "-monitor", "none", "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny", "-kernel", "/usr/local/share/srf-vm/Image", "-append", "console=ttyAMA0 root=PARTUUID=c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001 ro init=/init", "-drive", "file=" + c.Profile + ",if=none,format=raw,id=profile", "-device", "virtio-blk-device,drive=profile", "-drive", "file=/usr/local/share/srf-vm/root.raw,if=none,format=raw,readonly=on,id=root", "-device", "virtio-blk-device,drive=root", "-device", "virtio-serial-device"}
	for _, p := range []string{"control", "agent", "view"} {
		a = append(a, "-chardev", "socket,id="+p+",path="+filepath.Join(c.RuntimeDir, p+".sock")+",server=on,wait=off", "-device", "virtserialport,chardev="+p+",name=opensuperficies."+p)
	}
	return a, nil
}
