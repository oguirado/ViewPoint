package capture

import (
	"errors"
	"strings"
	"testing"
)

const testRam = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/slive-w2-a"

func uidCfg() RunConfig {
	return RunConfig{Slot: 0, Image: "opensuperficies-desktop-arm64:1.0.0", Iso: strings.Repeat("ab", 16),
		FBDir: testRam + "/fb", Entrypoint: "/repo/docker/cu/entrypoint-cu.sh",
		RamDir: testRam, ToolsDir: "/repo/build/slive/bin", OutDir: "/repo/build/slive/out", Geometry: "1920x1080x24"}
}

func wantGate(t *testing.T, err error, reason string) {
	t.Helper()
	var ce *Error
	if !errors.As(err, &ce) || ce.Class != ClassGate || ce.Reason != reason {
		t.Fatalf("want E_GATE %s, got %v", reason, err)
	}
}

func TestRunArgsNoUserFlag(t *testing.T) {
	c := uidCfg()
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	for _, a := range RunArgs(c) {
		if a == userFlag || a == "-u" || strings.HasPrefix(a, userFlag+"=") {
			t.Fatalf("user flag in the vector: %q", a)
		}
	}
	for _, extra := range [][]string{{userFlag, "1000:1000"}, {userFlag + "=1000"}, {"-u", "0"}, {"-u0"}} {
		c.ExtraArgs = extra
		wantGate(t, c.Validate(), "user_flag")
	}
}

func TestRunArgsTenantTmpfsUid10001(t *testing.T) {
	if CapsuleUID != 10001 {
		t.Fatal("CapsuleUID")
	}
	j := strings.Join(RunArgs(uidCfg()), " ")
	want := "--tmpfs /run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=0"
	if !strings.Contains(j, want) {
		t.Fatalf("missing %q in %s", want, j)
	}
}

func TestRunArgsMountsRamDirSamePath(t *testing.T) {
	c := uidCfg()
	j := strings.Join(RunArgs(c), " ")
	for _, want := range []string{"-v " + testRam + ":" + testRam + ":rw", "-v " + c.ToolsDir + ":/opt/cu:ro",
		"-v " + c.OutDir + ":/out:rw", "-v " + testRam + "/fb:/run/vqa/fb:rw", "-e GEOMETRY=1920x1080x24"} {
		if !strings.Contains(j, want) {
			t.Fatalf("missing %q in %s", want, j)
		}
	}
}

func TestRunConfigRefusesRamDirOutsideRamws(t *testing.T) {
	for _, bad := range []string{"/tmp/x", "/dev/shm/other/r1", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/../../x"} {
		c := uidCfg()
		c.RamDir = bad
		c.FBDir = bad + "/fb"
		wantGate(t, c.Validate(), "ramdir_outside_ramws")
	}
	c := uidCfg()
	c.OutDir = "/dev/shm/out"
	wantGate(t, c.Validate(), "out_in_ram")
}

func TestRunConfigRefusesFBOutsideRamDir(t *testing.T) {
	c := uidCfg()
	c.FBDir = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/other/fb"
	wantGate(t, c.Validate(), "fb_outside_ramdir")
}

// Golden of the W2 vector (RunArgsSHA256 recorded by the conductor in S-LIVE-1A v2).
const goldenW2 = "e862c11cfb63d1edc0bd130f4d5d2d4ffc581c3a82c1d57e7dfc6d23eaef8d85"

func TestRunArgsVectorShaGoldenW2(t *testing.T) {
	if got := RunArgsSHA256(RunArgs(uidCfg())); got != goldenW2 {
		t.Fatalf("RunArgsSHA256 = %s, golden %s", got, goldenW2)
	}
}

func TestRunConfigExtraArgsAllowlistOnly(t *testing.T) {
	for _, bad := range [][]string{
		{"-v", "/a:/b"}, {"-v", "/:/host:rw"}, {"--volume", "/a:/b"}, {"--entrypoint", "x"}, {"--entrypoint=/bin/sh"},
		{"--tmpfs", "/run/newton-tenant:rw,uid=0"}, {"--group-add", "0"}, {"--userns", "host"}, {"--cgroupns", "host"},
		{"--cap-drop", "NET_RAW"}, {"--privileged"}, {"--network", "host"}, {"--mount", "type=bind,src=/,dst=/h"},
		{"-e", "LD_PRELOAD=/x"}, {"-e"}, {"-e", "NOEQUALS"}, {"-e", "WM=qtile", "-v"}, {"WM=qtile"}, {"-v", "WM=qtile"}, {"--entrypoint=WM=x"}, {"-e", "WM"},
	} {
		c := uidCfg()
		c.ExtraArgs = bad
		wantGate(t, c.Validate(), "forbidden_arg")
	}
	c := uidCfg()
	c.ExtraArgs = []string{"-e", "WM=qtile"}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	a := RunArgs(c)
	if a[len(a)-3] != "-e" || a[len(a)-2] != "WM=qtile" || a[len(a)-1] != c.Image {
		t.Fatalf("extra env not placed before the image: %v", a)
	}
}

func TestRunConfigOutDirRefusesOtherTmpfsMount(t *testing.T) {
	if MountFSType("/run/lock/out") != "tmpfs" {
		t.Skip("/run/lock is not a tmpfs mount on this host")
	}
	c := uidCfg()
	c.OutDir = "/run/lock/out"
	err := c.Validate()
	var ce *Error
	if !errors.As(err, &ce) || ce.Reason != "out_in_ram" {
		t.Fatalf("want out_in_ram for a non-/dev/shm tmpfs OutDir, got %v", err)
	}
}

func TestRunConfigFBDirUnderRamRootWithoutRamDir(t *testing.T) {
	c := uidCfg()
	c.RamDir = ""
	c.FBDir = "/tmp/outside_fb"
	wantGate(t, c.Validate(), "fb_outside_ramdir")
	c.FBDir = DefaultRamRoot // the root itself is not a run dir
	wantGate(t, c.Validate(), "fb_outside_ramdir")
	c.FBDir = testRam + "/fb"
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestRunConfigExtraArgsEnvKeyAllowlist(t *testing.T) {
	c := uidCfg()
	c.ExtraArgs = []string{"-e", "LD_PRELOAD=/x"}
	wantGate(t, c.Validate(), "forbidden_arg")
	c.ExtraArgs = []string{"-e", "WM=qtile", "-e", "PATH=/x"}
	wantGate(t, c.Validate(), "forbidden_arg")
}
