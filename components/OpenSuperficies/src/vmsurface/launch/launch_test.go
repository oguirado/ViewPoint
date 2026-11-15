package launch

import (
	"reflect"
	"strings"
	"testing"
)

func TestArgsGolden(t *testing.T) {
	got, err := Args(Config{CPUs: 4, MemoryMiB: 6144, RuntimeDir: "/run/newton-tenant", Profile: "/profile/disk.ext4"})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"-machine", "virt-8.2,accel=kvm,gic-version=host", "-cpu", "host,pmu=off", "-smp", "4", "-m", "6144", "-nodefaults", "-no-user-config", "-display", "none", "-monitor", "none", "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny", "-kernel", "/usr/local/share/srf-vm/Image", "-append", "console=ttyAMA0 root=PARTUUID=c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001 ro init=/init", "-drive", "file=/profile/disk.ext4,if=none,format=raw,id=profile", "-device", "virtio-blk-device,drive=profile", "-drive", "file=/usr/local/share/srf-vm/root.raw,if=none,format=raw,readonly=on,id=root", "-device", "virtio-blk-device,drive=root", "-device", "virtio-serial-device"}
	for _, p := range []string{"control", "agent", "view"} {
		want = append(want, "-chardev", "socket,id="+p+",path=/run/newton-tenant/"+p+".sock,server=on,wait=off", "-device", "virtserialport,chardev="+p+",name=opensuperficies."+p)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("argv\ngot %q\nwant %q", got, want)
	}
	if strings.Contains(strings.Join(got, " "), "netdev") {
		t.Fatal("network device")
	}
}
func TestArgsRootIsLastVirtioBlockDevice(t *testing.T) {
	args, err := Args(Config{CPUs: 4, MemoryMiB: 6144, RuntimeDir: "/run/newton-tenant", Profile: "/profile/disk.ext4"})
	if err != nil {
		t.Fatal(err)
	}
	var devices []string
	for i := 0; i+1 < len(args); i++ {
		if args[i] == "-device" && strings.HasPrefix(args[i+1], "virtio-blk-device,") {
			devices = append(devices, args[i+1])
		}
	}
	if len(devices) != 2 || devices[len(devices)-1] != "virtio-blk-device,drive=root" {
		t.Fatalf("root must be the last virtio-blk device, got %q", devices)
	}
}

func TestArgsReject(t *testing.T) {
	for _, c := range []Config{{}, {CPUs: 6, MemoryMiB: 6144, RuntimeDir: "/run/newton-tenant", Profile: "/profile/disk.ext4"}, {CPUs: 4, MemoryMiB: 7169, RuntimeDir: "/run/newton-tenant", Profile: "/profile/disk.ext4"}, {CPUs: 4, MemoryMiB: 6144, RuntimeDir: "/tmp,bad", Profile: "/profile/disk.ext4"}} {
		if _, err := Args(c); err == nil {
			t.Fatalf("accepted %+v", c)
		}
	}
}

func TestArgsRootSelectionIndependentOfDeviceName(t *testing.T) {
	args, err := Args(Config{CPUs: 4, MemoryMiB: 6144, RuntimeDir: "/run/newton-tenant", Profile: "/profile/disk.ext4"})
	if err != nil {
		t.Fatal(err)
	}
	for i, arg := range args {
		if strings.Contains(arg, "root=/dev/") {
			t.Fatalf("device-dependent root: %q", arg)
		}
		if arg == "-append" && args[i+1] != "console=ttyAMA0 root=PARTUUID=c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001 ro init=/init" {
			t.Fatalf("unexpected cmdline: %q", args[i+1])
		}
	}
}
