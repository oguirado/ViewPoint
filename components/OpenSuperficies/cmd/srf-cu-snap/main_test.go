package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"testing"
)

func runCLI(args ...string) (int, string, string) {
	var o, e bytes.Buffer
	c := run(args, &o, &e)
	return c, o.String(), e.String()
}

func TestNoRc1(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	if regexp.MustCompile(`os\.Exit\(\s*1\s*\)|return\s+1\b`).Match(src) {
		t.Fatal("rc 1 in source")
	}
	out := t.TempDir()
	cases := [][]string{
		{},
		{"--bogus"},
		{"--fake-fb", "--ramws-dir", "/tmp/not-ram", "--out", out},
		{"--fake-fb", "--ramws-dir", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/x", "--out", "/dev/shm/evidence"},
		{"--fb", "/nonexistent", "--ramws-dir", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/x", "--out", out, "--display", ":0"},
		{"--fake-fb", "--frames", "0", "--ramws-dir", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/x", "--out", out},
	}
	for _, a := range cases {
		c, _, _ := runCLI(a...)
		if c == 1 || c == 0 || c > 7 {
			t.Fatalf("%v -> rc %d", a, c)
		}
	}
}

func TestNonRamDirRc5(t *testing.T) {
	c, _, e := runCLI("--fake-fb", "--ramws-dir", t.TempDir(), "--out", t.TempDir())
	if c != 5 || !strings.Contains(e, "E_RAM_NOT_TMPFS") {
		t.Fatalf("rc %d %s", c, e)
	}
}

func TestHostDisplayRc5(t *testing.T) {
	c, _, e := runCLI("--fb", "/nonexistent", "--display", ":0", "--ramws-dir", managedRun(t, "fl1-hostdisp0"), "--out", t.TempDir())
	if c != 5 || !strings.Contains(e, "host_display") {
		t.Fatalf("rc %d %s", c, e)
	}
}

func TestFakeFBEndToEnd(t *testing.T) {
	dir := managedRun(t, "testL1-fakefb")
	var dets []string
	for i := 0; i < 2; i++ {
		out := t.TempDir()
		c, so, se := runCLI("--fake-fb", "--frames", "8", "--ramws-dir", dir, "--out", out)
		if c != 0 {
			t.Fatalf("rc %d %s", c, se)
		}
		for _, f := range []string{"frames.manifest.json", "capture_receipt.json"} {
			b, err := os.ReadFile(filepath.Join(out, f))
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(b), "/dev/shm") {
				t.Fatalf("%s leaks /dev/shm path", f)
			}
		}
		var r map[string]any
		json.Unmarshal([]byte(so), &r)
		dets = append(dets, r["det_sha256"].(string))
		if r["frames"] != float64(8) {
			t.Fatalf("frames %v", r["frames"])
		}
	}
	if dets[0] != dets[1] {
		t.Fatal("det_sha256 differs across runs")
	}
	if _, err := os.Stat(filepath.Join(dir, "frames.raw")); err != nil {
		t.Fatal(err)
	}
}

const lawRoot = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"

// managedRun mimics a live vision/ramws_shim.py session: a direct child of the law root with owner.json and a flock-held .lock.
func managedRun(t *testing.T, name string) string {
	t.Helper()
	if st, err := os.Stat("/dev/shm"); err != nil || !st.IsDir() {
		t.Skip("no /dev/shm")
	}
	if err := os.MkdirAll(lawRoot, 0o700); err != nil {
		t.Skip(err)
	}
	dir := filepath.Join(lawRoot, name)
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	os.WriteFile(filepath.Join(dir, "owner.json"), []byte("{}"), 0o600)
	lk, err := os.OpenFile(filepath.Join(dir, ".lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Flock(int(lk.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lk.Close() })
	return dir
}

// JW-03: the ramws root is not caller-controlled.
func TestSnapRamwsRootNotOverridable(t *testing.T) {
	leak := "/dev/shm/fl1-leak-" + filepath.Base(t.TempDir())
	t.Cleanup(func() { os.RemoveAll(leak) })
	c, _, _ := runCLI("--fake-fb", "--frames", "2", "--ramws-root", "/dev/shm", "--ramws-dir", leak, "--out", t.TempDir())
	if c != 2 && c != 5 {
		t.Fatalf("rc %d", c)
	}
	if _, err := os.Stat(filepath.Join(leak, "frames.raw")); err == nil {
		t.Fatal("frames.raw written outside the law root")
	}
	// a tmpfs dir that is NOT a direct child of the law root is refused even without the flag
	nested := filepath.Join(managedRun(t, "fl1-nest"), "inner")
	os.Mkdir(nested, 0o700)
	if c, _, _ := runCLI("--fake-fb", "--ramws-dir", nested, "--out", t.TempDir()); c != 5 {
		t.Fatalf("nested run dir rc %d", c)
	}
}

// ADC-02: unmanaged run dirs (no owner.json/.lock session) and symlinked --out into RAM are refused.
func TestSnapRefusesUnmanagedRamwsAndSymlinkOut(t *testing.T) {
	if _, err := os.Stat("/dev/shm"); err != nil {
		t.Skip("no /dev/shm")
	}
	os.MkdirAll(lawRoot, 0o700)
	un := filepath.Join(lawRoot, "fl1-unmanaged")
	os.Mkdir(un, 0o700)
	t.Cleanup(func() { os.RemoveAll(un) })
	c, _, e := runCLI("--fake-fb", "--ramws-dir", un, "--out", t.TempDir())
	if c != 5 || !strings.Contains(e, "unmanaged_run_dir") {
		t.Fatalf("unmanaged: rc %d %s", c, e)
	}
	if _, err := os.Stat(filepath.Join(un, "frames.raw")); err == nil {
		t.Fatal("frames.raw written into an unmanaged dir")
	}
	// missing dir is not created
	missing := filepath.Join(lawRoot, "fl1-missing")
	if c, _, _ := runCLI("--fake-fb", "--ramws-dir", missing, "--out", t.TempDir()); c == 0 {
		t.Fatal("missing run dir accepted")
	}
	if _, err := os.Stat(missing); err == nil {
		os.RemoveAll(missing)
		t.Fatal("unmanaged run dir was created")
	}
	// --out symlink into /dev/shm
	dir := managedRun(t, "fl1-symout")
	ln := filepath.Join(t.TempDir(), "s")
	os.Symlink("/dev/shm", ln)
	target := filepath.Join(ln, "fl1-out-"+filepath.Base(ln))
	t.Cleanup(func() { os.RemoveAll(target) })
	c, _, _ = runCLI("--fake-fb", "--ramws-dir", dir, "--out", target)
	if c != 2 {
		t.Fatalf("symlinked out rc %d", c)
	}
	if _, err := os.Stat(filepath.Join(target, "capture_receipt.json")); err == nil {
		t.Fatal("evidence written into /dev/shm via symlink")
	}
}

// JW-07: the capsule marker is not a flag.
func TestSnapInCapsuleNotAFlag(t *testing.T) {
	c, _, e := runCLI("--fb", "/nonexistent", "--display", ":100", "--in-capsule", "--ramws-dir", managedRun(t, "fl1-capflag"), "--out", t.TempDir())
	if c != 2 || !strings.Contains(e, "not defined") {
		t.Fatalf("rc %d %s", c, e)
	}
}

func TestHostDisplayRefusedWithoutNetns(t *testing.T) {
	if _, err := os.Stat("/.dockerenv"); err == nil {
		t.Skip("running inside a container")
	}
	c, _, e := runCLI("--fb", "/nonexistent", "--display", ":100", "--ramws-dir", managedRun(t, "fl1-hostdisp"), "--out", t.TempDir())
	if c != 5 || !strings.Contains(e, "host_display") {
		t.Fatalf("rc %d %s", c, e)
	}
}

// ADW1-05: the synthetic framebuffer is a test fixture: without OS_CU_TEST=1 it is a usage error.
func TestFakeFBRefusedWithoutTestEnv(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	dir := managedRun(t, "fw1-fakeenv0")
	c, _, e := runCLI("--fake-fb", "--frames", "2", "--ramws-dir", dir, "--out", t.TempDir())
	if c != 2 || !strings.Contains(e, "fake_requires_test_env") {
		t.Fatalf("rc %d %s", c, e)
	}
	if _, err := os.Stat(filepath.Join(dir, "frames.raw")); err == nil {
		t.Fatal("refused run wrote frames.raw")
	}
}

// ADW1-04: an OS capture leaves raw.json + frames.linear.raw in the ramws session so GuiAdapter can read it.
func TestSnapWritesRawJSON(t *testing.T) {
	dir := managedRun(t, "fw1-rawjson0")
	if c, _, e := runCLI("--fake-fb", "--frames", "3", "--ramws-dir", dir, "--out", t.TempDir()); c != 0 {
		t.Fatalf("rc %d %s", c, e)
	}
	b, err := os.ReadFile(filepath.Join(dir, "raw.json"))
	if err != nil {
		t.Fatalf("no raw.json: %v", err)
	}
	var rj struct {
		Width, Height, Stride int
		File                  string
	}
	if err := json.Unmarshal(b, &rj); err != nil || rj.File != "frames.linear.raw" || rj.Stride != rj.Width*3 {
		t.Fatalf("raw.json %s %v", b, err)
	}
	if st, err := os.Stat(filepath.Join(dir, rj.File)); err != nil || st.Size() != int64(3*rj.Width*rj.Height*3) {
		t.Fatalf("linear file %v %v", st, err)
	}
}

func TestMain(m *testing.M) {
	os.Setenv("OS_CU_TEST", "1")
	os.Exit(m.Run())
}
