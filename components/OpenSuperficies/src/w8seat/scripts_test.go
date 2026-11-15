package w8seat

import (
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSetupRollbackCleanWithSeatSocket(t *testing.T) {
	// AF_UNIX paths are limited to 108 bytes: keep socket fixtures under a short /tmp root, not the repository tree.
	scratch := "/tmp"
	root, e := os.MkdirTemp(scratch, "r-")
	if e != nil {
		t.Fatal(e)
	}
	defer os.RemoveAll(root)
	bindir := filepath.Join(root, "bin")
	runDir := filepath.Join(root, "opensuperficies")
	for _, p := range []string{bindir, runDir, filepath.Join(root, "etc")} {
		if e = os.MkdirAll(p, 0750); e != nil {
			t.Fatal(e)
		}
	}
	for name, body := range map[string]string{"id": "#!/bin/sh\nprintf '0\\n'\n", "getent": "#!/bin/sh\nexit 2\n", "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$W8_STUB_LOG\"\nexit 0\n"} {
		if e = os.WriteFile(filepath.Join(bindir, name), []byte(body), 0755); e != nil {
			t.Fatal(e)
		}
	}
	socket := filepath.Join(runDir, "w8-seat.sock")
	l, e := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if e != nil {
		t.Fatal(e)
	}
	defer l.Close()
	script := os.Getenv("W8_BASELINE_SETUP")
	if script == "" {
		script = "../../scripts/w8/osworker-setup.sh"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/sh", script, "--rollback")
	cmd.Env = []string{"PATH=" + bindir + ":/usr/bin:/bin", "OSW_ROOT=" + root, "OSW_RUN_ROOT=" + root, "W8_STUB_LOG=" + filepath.Join(root, "calls"), "LC_ALL=C"}
	b, e := cmd.CombinedOutput()
	if e != nil {
		t.Fatalf("rollback: %v %s", e, b)
	}
	if _, e = os.Lstat(socket); !os.IsNotExist(e) {
		t.Fatal("socket survived rollback", e)
	}
	if _, e = os.Lstat(runDir); !os.IsNotExist(e) {
		t.Fatal("runtime directory survived rollback", e)
	}
}
func TestSetupRejectsUnpinnedSeatBinary(t *testing.T) {
	root := t.TempDir()
	binary := filepath.Join(root, "seat")
	if e := os.WriteFile(binary, []byte("fake conductor binary"), 0555); e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/sh", "../../scripts/w8/osworker-setup.sh", "--seat-bin", binary, "--seat-sha256", "0000000000000000000000000000000000000000000000000000000000000000")
	b, e := cmd.CombinedOutput()
	x, ok := e.(*exec.ExitError)
	if !ok || x.ExitCode() != 5 {
		t.Fatalf("wanted pin refusal5: %s %v", b, e)
	}
}

// Live run 2026-10-20: in a systemd *user* unit, mount-namespace sandboxing implies PrivateUsers=yes, which maps
// root-owned trusted files to nobody; the seat then refuses them ("trusted file owner", rc 4) in a restart loop.
func TestSeatUserUnitHasNoImplicitUserNamespace(t *testing.T) {
	b, e := os.ReadFile("../../scripts/w8/w8-seat.service")
	if e != nil {
		t.Fatal(e)
	}
	for _, line := range strings.Split(string(b), "\n") {
		k := strings.TrimSpace(strings.SplitN(line, "=", 2)[0])
		switch k {
		case "ProtectSystem", "ProtectHome", "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "PrivateTmp", "PrivateUsers", "PrivateDevices", "ProtectKernelTunables", "ProtectControlGroups", "TemporaryFileSystem", "BindPaths", "BindReadOnlyPaths":
			t.Fatalf("user unit option %s implies a user namespace", k)
		}
	}
}
