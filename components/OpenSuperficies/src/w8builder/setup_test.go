package w8builder

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type shellFixture struct {
	root, state string
	env         []string
}

func newShellFixture(t *testing.T) shellFixture {
	t.Helper()
	// AF_UNIX paths are limited to 108 bytes: keep socket fixtures under a short /tmp root, not the repository tree.
	scratch := "/tmp"
	root, e := os.MkdirTemp(scratch, "s-")
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	for _, n := range []string{"etc", "state", "sys/fs/cgroup", "sys/devices/system/cpu"} {
		if e = os.MkdirAll(filepath.Join(root, n), 0755); e != nil {
			t.Fatal(e)
		}
	}
	for _, n := range []string{"subuid", "subgid"} {
		if e = os.WriteFile(filepath.Join(root, "etc", n), []byte("admin:100000:65536\n"), 0644); e != nil {
			t.Fatal(e)
		}
	}
	if e = os.WriteFile(filepath.Join(root, "sys/fs/cgroup/cgroup.controllers"), []byte("cpu cpuset io memory pids\n"), 0644); e != nil {
		t.Fatal(e)
	}
	fake, e := filepath.Abs("../../testdata/w8builder/fakebin")
	if e != nil {
		t.Fatal(e)
	}

	repo, e := filepath.Abs("../..")
	if e != nil {
		t.Fatal(e)
	}
	// Copy inert wrappers into this lane's scratch; never execute host admin tools.
	localFake := filepath.Join(root, "fakebin")
	if e = os.MkdirAll(localFake, 0700); e != nil {
		t.Fatal(e)
	}
	entries, e := os.ReadDir(fake)
	if e != nil {
		t.Fatal(e)
	}
	for _, entry := range entries {
		data, e := os.ReadFile(filepath.Join(fake, entry.Name()))
		if e != nil {
			t.Fatal(e)
		}
		if entry.Name() == "_fake.py" {
			data, e = os.ReadFile("../../testdata/w8seat/operator_fake.py")
			if e != nil {
				t.Fatal(e)
			}
		}
		if e = os.WriteFile(filepath.Join(localFake, entry.Name()), data, 0700); e != nil {
			t.Fatal(e)
		}
	}
	fake = localFake
	if e = os.WriteFile(filepath.Join(root, "sys/devices/system/cpu/online"), []byte("0-19\n"), 0644); e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(filepath.Join(root, "seat"), []byte("inert fixture seat binary\n"), 0555); e != nil {
		t.Fatal(e)
	}
	return shellFixture{root: root, state: filepath.Join(root, "state"), env: []string{"OSW_REPO=" + repo, "PATH=" + fake + ":/usr/bin:/bin", "LC_ALL=C", "OSW_ROOT=" + root, "OSW_ETC=" + filepath.Join(root, "etc"), "OSW_FAKE_STATE=" + filepath.Join(root, "state"), "HOME=" + root, "TMPDIR=" + root, "PYTHONDONTWRITEBYTECODE=1"}}
}
func (f shellFixture) run(t *testing.T, script string, args ...string) (int, string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	if script == "osworker-setup.sh" && len(args) == 0 {
		data, e := os.ReadFile(filepath.Join(f.root, "seat"))
		if e != nil {
			t.Fatal(e)
		}
		args = []string{"--seat-bin", filepath.Join(f.root, "seat"), "--seat-sha256", fmt.Sprintf("%x", sha256.Sum256(data))}
	}
	argv := append([]string{"../../scripts/w8/" + script}, args...)
	cmd := exec.CommandContext(ctx, "/bin/sh", argv...)
	cmd.Env = f.env
	b, e := cmd.CombinedOutput()
	if ctx.Err() != nil {
		t.Fatalf("script deadline: %s %s", script, b)
	}
	if e == nil {
		return 0, string(b)
	}
	if ex, ok := e.(*exec.ExitError); ok {
		return ex.ExitCode(), string(b)
	}
	t.Fatal(e)
	return 6, ""
}
func TestSetupScriptIdempotentSubgidAndRollback(t *testing.T) {
	f := newShellFixture(t)
	if rc, out := f.run(t, "osworker-setup.sh"); rc != 0 {
		t.Fatalf("setup rc%d %s", rc, out)
	}
	if rc, out := f.run(t, "osworker-verify.sh"); rc != 0 {
		t.Fatalf("verify rc%d %s", rc, out)
	}
	installed, e := os.ReadFile(filepath.Join(f.root, "usr/local/libexec/opensuperficies/srf-w8-seat"))
	original, _ := os.ReadFile(filepath.Join(f.root, "seat"))
	if e != nil || string(installed) != string(original) {
		t.Fatal("installed seat differs from pinned binary", e)
	}
	cpu, e := os.ReadFile(filepath.Join(f.root, "etc/opensuperficies/w8_cpus.json"))
	if e != nil || !strings.Contains(string(cpu), `"worker": "10-14"`) || !strings.Contains(string(cpu), `"interactive": "0,1,2,3,4,5,6,7,8,9,15,16,17,18,19"`) {
		t.Fatal("CPU partition missing", e, string(cpu))
	}
	unit, e := os.ReadFile(filepath.Join(f.root, "var/lib/osworker/.config/systemd/user/w8-seat.service"))
	if e != nil || !strings.Contains(string(unit), "ExecStart=/usr/local/libexec/opensuperficies/srf-w8-seat") {
		t.Fatal("seat service absent", e)
	}
	if e := os.Remove(filepath.Join(f.root, "etc/subgid")); e != nil {
		t.Fatal(e)
	}
	if rc, _ := f.run(t, "osworker-verify.sh"); rc != 5 {
		t.Fatalf("missing subgid rc%d", rc)
	}
	if e := os.WriteFile(filepath.Join(f.root, "etc/subgid"), []byte("admin:100000:65536\n"), 0644); e != nil {
		t.Fatal(e)
	}
	if rc, out := f.run(t, "osworker-setup.sh"); rc != 0 {
		t.Fatalf("repair rc%d %s", rc, out)
	}
	if rc, out := f.run(t, "osworker-setup.sh"); rc != 0 {
		t.Fatalf("rerun rc%d %s", rc, out)
	}
	for _, db := range []string{"subuid", "subgid"} {
		b, e := os.ReadFile(filepath.Join(f.root, "etc", db))
		if e != nil {
			t.Fatal(e)
		}
		if strings.Count(string(b), "osworker:165536:65536\n") != 1 {
			t.Fatalf("duplicate/missing %s: %s", db, b)
		}
	}
	calls, _ := os.ReadFile(filepath.Join(f.state, "calls"))
	if strings.Count(string(calls), "useradd ") != 1 {
		t.Fatal("rerun recreated user")
	}
	if strings.Count(string(calls), "usermod --add-subgids ") != 2 {
		t.Fatal("subgid rerun was not no-op")
	}
	if rc, out := f.run(t, "osworker-setup.sh", "--rollback"); rc != 0 {
		t.Fatalf("rollback rc%d %s", rc, out)
	}
	for _, n := range []string{"etc/systemd/system/user@.service.d/delegate.conf", "etc/tmpfiles.d/opensuperficies-w8.conf", "var/lib/osworker/.config/systemd/user/docker.service.d/w8.conf"} {
		if _, e := os.Stat(filepath.Join(f.root, n)); !os.IsNotExist(e) {
			t.Fatalf("marker survived: %s", n)
		}
	}
	for _, db := range []string{"subuid", "subgid"} {
		b, _ := os.ReadFile(filepath.Join(f.root, "etc", db))
		if string(b) != "admin:100000:65536\n" {
			t.Fatalf("rollback changed admin range: %s", b)
		}
	}
	calls, _ = os.ReadFile(filepath.Join(f.state, "calls"))
	if !strings.Contains(string(calls), "gpasswd -d osworker kvm") {
		t.Fatal("rollback missed kvm")
	}
	if strings.Contains(string(calls), "systemctl stop docker") {
		t.Fatal("rootful daemon touched")
	}
}
func TestKillSwitchOffOnRoundTrip(t *testing.T) {
	f := newShellFixture(t)
	if rc, out := f.run(t, "osworker-setup.sh"); rc != 0 {
		t.Fatalf("setup rc%d %s", rc, out)
	}
	if rc, out := f.run(t, "osworker-kill.sh", "off"); rc != 0 || !strings.Contains(out, "killswitch=off") {
		t.Fatalf("off rc%d %s", rc, out)
	}
	if rc, _ := f.run(t, "osworker-verify.sh"); rc != 5 {
		t.Fatal("off verify passed")
	}
	if rc, out := f.run(t, "osworker-kill.sh", "on"); rc != 0 || !strings.Contains(out, "VERIFY=PASS") {
		t.Fatalf("on rc%d %s", rc, out)
	}
	calls, _ := os.ReadFile(filepath.Join(f.state, "calls"))
	if !strings.Contains(string(calls), "systemctl stop user@"+"10001.service") {
		t.Fatal("dedicated user service not stopped")
	}
	if strings.Contains(string(calls), "systemctl stop docker") {
		t.Fatal("rootful service stopped")
	}
}
func TestSetupRootlessExtrasMatchesInstalledVersion(t *testing.T) {
	f := newShellFixture(t)
	f.env = append(f.env, "OSW_FAKE_AVAILABLE_VERSION=5:other")
	rc, out := f.run(t, "osworker-setup.sh")
	if rc != 4 || !strings.Contains(out, "Operator options") {
		t.Fatalf("mismatch rc%d %s", rc, out)
	}
	calls, _ := os.ReadFile(filepath.Join(f.state, "calls"))
	if strings.Contains(string(calls), "apt-get ") || strings.Contains(string(calls), "useradd ") {
		t.Fatal("package mismatch mutated setup")
	}
}

func TestKillSwitchProbeErrorIsNotAbsence(t *testing.T) {
	f := newShellFixture(t)
	if rc, out := f.run(t, "osworker-setup.sh"); rc != 0 {
		t.Fatalf("setup rc%d %s", rc, out)
	}
	f.env = append(f.env, "OSW_FAKE_FAIL=pgrep", "OSW_FAKE_RC=2")
	if rc, out := f.run(t, "osworker-kill.sh", "off"); rc != 5 || !strings.Contains(out, "process query failed") {
		t.Fatalf("probe error treated as absence rc%d %s", rc, out)
	}
}
func TestVerifyFindFailureCannotReportZero(t *testing.T) {
	f := newShellFixture(t)
	if rc, out := f.run(t, "osworker-setup.sh"); rc != 0 {
		t.Fatalf("setup rc%d %s", rc, out)
	}
	f.env = append(f.env, "OSW_FAKE_FAIL=find", "OSW_FAKE_RC=5")
	if rc, out := f.run(t, "osworker-verify.sh"); rc != 5 || strings.Contains(out, "home_root_owned=0") {
		t.Fatalf("find error treated as zero rc%d %s", rc, out)
	}
}

func TestSetupRequiresExactDockerRelease(t *testing.T) {
	for _, v := range []string{"5:29.6.20-1~ubuntu.24.04~noble", "5:129.6.2-1~ubuntu.24.04~noble", "5:29.6.2evil-1"} {
		t.Run(v, func(t *testing.T) {
			f := newShellFixture(t)
			f.env = append(f.env, "OSW_FAKE_VERSION="+v)
			if rc, out := f.run(t, "osworker-setup.sh"); rc != 4 {
				t.Fatalf("version %s accepted rc%d: %s", v, rc, out)
			}
			calls, _ := os.ReadFile(filepath.Join(f.state, "calls"))
			if strings.Contains(string(calls), "apt-get ") || strings.Contains(string(calls), "useradd ") {
				t.Fatal("invalid version mutated setup")
			}
		})
	}
}
