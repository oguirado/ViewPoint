package capture

import (
	"strings"
	"testing"
)

// W3 lane C: the Xvfb cursor switch (WL-7) is an allow-listed env key with a closed value; the scenes arrive through
// ToolsDir (/opt/cu), so the vector gains no new mount and mounts stay refused in ExtraArgs.
func TestRunArgsXvfbCursorEnvAllowed(t *testing.T) {
	c := uidCfg()
	c.ExtraArgs = []string{"-e", "XVFB_CURSOR=on"}
	if err := c.Validate(); err != nil {
		t.Fatalf("XVFB_CURSOR=on refused: %v", err)
	}
	a := RunArgs(c)
	found := false
	for i := 0; i+1 < len(a); i++ {
		if a[i] == "-e" && a[i+1] == "XVFB_CURSOR=on" {
			found = true
		}
	}
	if !found {
		t.Fatalf("XVFB_CURSOR=on not in vector: %v", a)
	}
	c.ExtraArgs = []string{"-e", "XVFB_CURSOR=off"}
	if err := c.Validate(); err != nil {
		t.Fatalf("XVFB_CURSOR=off refused: %v", err)
	}
	for _, bad := range []string{"XVFB_CURSOR=1", "XVFB_CURSOR=", "XVFB_CURSOR=on;x", "XVFB_CURSOR=ON"} {
		c.ExtraArgs = []string{"-e", bad}
		wantGate(t, c.Validate(), "forbidden_arg")
	}
}

func TestRunArgsScenesViaToolsDirNoExtraMount(t *testing.T) {
	c := uidCfg()
	c.ExtraArgs = []string{"-e", "WM=openbox", "-e", "XVFB_CURSOR=on"}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	a := RunArgs(c)
	mounts := 0
	for i := 0; i+1 < len(a); i++ {
		if a[i] == "-v" {
			mounts++
			if strings.Contains(a[i+1], "scenes") {
				t.Fatalf("scenes mounted separately: %s", a[i+1])
			}
		}
	}
	if mounts != 5 { // fb, ram, tools(/opt/cu), out, entrypoint
		t.Fatalf("want 5 mounts, got %d: %v", mounts, a)
	}
	for _, x := range [][]string{{"-v", "/h/scenes:/opt/cu/scenes/v2:ro"}, {"--tmpfs", "/x"}, {"-e", "XVFB_CURSOR"}} {
		c.ExtraArgs = x
		wantGate(t, c.Validate(), "forbidden_arg")
	}
}

// FX2 CF-03: the cpuset is a validated RunConfig field; os-cu-0 owns cpus 10-14 (0,1 stays allowed and is the default).
func cpusetOf(t *testing.T, a []string) string {
	t.Helper()
	n, v := 0, ""
	for i := 0; i+1 < len(a); i++ {
		if a[i] == "--cpuset-cpus" {
			n, v = n+1, a[i+1]
		}
	}
	if n != 1 {
		t.Fatalf("want exactly one --cpuset-cpus, got %d: %v", n, a)
	}
	return v
}

// Golden of the FX2 W3 vector: uidCfg() with CPUSet "10-14".
const goldenW3CPU1014 = "d22ca360fa41135d9faedba07bcce0fc609e21508c948aa2ea95d50fadf29c7e"

func TestRunConfigCPUSetAllowList(t *testing.T) {
	for _, tc := range []struct{ in, want string }{{"", "0,1"}, {"0,1", "0,1"}, {"10-14", "10-14"}} {
		c := uidCfg()
		c.CPUSet = tc.in
		if err := c.Validate(); err != nil {
			t.Fatalf("CPUSet %q refused: %v", tc.in, err)
		}
		if got := cpusetOf(t, RunArgs(c)); got != tc.want {
			t.Fatalf("CPUSet %q -> --cpuset-cpus %q, want %q", tc.in, got, tc.want)
		}
	}
	for _, bad := range []string{"0-19", "15-19", "10-15", "0", "1,0", "10,11,12,13,14", " 10-14", "10-14 ", "10-14,15", "0,1 --privileged", "all"} {
		c := uidCfg()
		c.CPUSet = bad
		wantGate(t, c.Validate(), "cpuset_not_allowed")
	}
}

func TestRunArgsVectorShaGoldenW3CPUSet(t *testing.T) {
	c := uidCfg()
	c.CPUSet = "10-14"
	got := RunArgsSHA256(RunArgs(c))
	if got != goldenW3CPU1014 {
		t.Fatalf("RunArgsSHA256(10-14) = %s, golden %s", got, goldenW3CPU1014)
	}
	if got == goldenW2 {
		t.Fatal("cpuset 10-14 not in vector")
	}
	c.CPUSet = "0,1"
	if g := RunArgsSHA256(RunArgs(c)); g != goldenW2 {
		t.Fatalf("explicit 0,1 changed the W2 vector: %s", g)
	}
}
