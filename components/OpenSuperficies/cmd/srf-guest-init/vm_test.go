package main

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestDiscoverPorts(t *testing.T) {
	root := t.TempDir()
	for dev, name := range map[string]string{"vport0p1": controlPort, "vport0p2": agentPort, "vport0p3": viewPort} {
		p := filepath.Join(root, dev)
		if err := os.Mkdir(p, 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(p, "name"), []byte(name+"\n"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	ports, err := discoverPorts(root, "/fake/dev")
	if err != nil {
		t.Fatal(err)
	}
	if ports[agentPort] != "/fake/dev/vport0p2" {
		t.Fatal(ports)
	}
	p := filepath.Join(root, "duplicate")
	os.Mkdir(p, 0755)
	os.WriteFile(filepath.Join(p, "name"), []byte(agentPort), 0600)
	if _, err = discoverPorts(root, "/fake/dev"); err == nil {
		t.Fatal("duplicate accepted")
	}
}

func TestAbsentAgentPreservesLegacy(t *testing.T) {
	ports, err := discoverPorts(filepath.Join(t.TempDir(), "absent"), "/dev")
	if err != nil || ports[agentPort] != "" {
		t.Fatalf("%v %v", ports, err)
	}
	if err := requireVMPorts(map[string]string{controlPort: "control"}); err == nil {
		t.Fatal("incomplete VM ports accepted")
	}
}

func TestBootEnvironment(t *testing.T) {
	good := `{"SRF_PROGRAM":"notepad-vm","SRF_WINDOWS":"1","SRF_TILES":"[{\"id\":\"w1\",\"x\":0,\"y\":0,\"w\":400,\"h\":300}]","NO_AT_BRIDGE":"1"}`
	env, err := parseBootEnv([]byte(good))
	if err != nil {
		t.Fatal(err)
	}
	if !contains(env, "SRF_PROGRAM=notepad-vm") || !contains(env, "HOME=/profile") {
		t.Fatal(env)
	}
	for _, bad := range []string{`{}`, strings.Replace(good, `"1"`, `"0"`, 1), strings.Replace(good, "notepad-vm", "bad", 1), strings.Replace(good, `"NO_AT_BRIDGE":"1"`, `"NO_AT_BRIDGE":"secret"`, 1), strings.TrimSuffix(good, "}") + `,"PATH":"evil"}`, good + ` {}`} {
		if _, err := parseBootEnv([]byte(bad)); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}
func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

func TestPrepareGuestMountsAndFsck(t *testing.T) {
	var calls []string
	ops := bootOps{
		profile: func() (string, error) { return "/fake/labelled-profile", nil },
		mkdir:   func(p string, m os.FileMode) error { return nil },
		chown: func(p string, u, g int) error {
			if u != 10001 || g != 10001 {
				t.Fatal(u, g)
			}
			return nil
		},
		chmod: func(string, os.FileMode) error { return nil },
		mount: func(src, dst, kind, data string) error {
			calls = append(calls, src+" "+dst+" "+kind+" "+data)
			return nil
		},
		check: func(argv []string) error { calls = append(calls, strings.Join(argv, " ")); return nil },
	}
	if err := prepareGuest(ops); err != nil {
		t.Fatal(err)
	}
	if !contains(calls, "tmpfs /run/vqa/fb tmpfs size=64m,mode=0700,uid=10001,gid=10001") {
		t.Fatal(calls)
	}
	fsck, mount := -1, -1
	for i, c := range calls {
		if c == "/sbin/fsck -p /fake/labelled-profile" {
			fsck = i
		}
		if c == "/fake/labelled-profile /profile ext4 " {
			mount = i
		}
	}
	if fsck < 0 || mount <= fsck {
		t.Fatal(calls)
	}
	ops.check = func([]string) error { return errors.New("uncorrected profile") }
	calls = nil
	if prepareGuest(ops) == nil {
		t.Fatal("fsck failure accepted")
	}
	for _, c := range calls {
		if strings.HasPrefix(c, "/fake/labelled-profile ") {
			t.Fatal("mounted failed fsck")
		}
	}
}

func TestControlRelayAndShutdown(t *testing.T) {
	a, b := net.Pipe()
	defer b.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	var commands []commandSpec
	stopped := make(chan struct{}, 3)
	ops := fakeBootOps()
	ops.start = func(s commandSpec) (childProcess, error) {
		commands = append(commands, s)
		ch := make(chan error, 1)
		if s.Path == "/usr/local/bin/identity-entrypoint.sh" {
			go func() { io.WriteString(s.Stdout, "xpra layer up\nPROGRAM_READY {\"windows\":[]}\n") }()
		}
		return childProcess{done: ch, stop: func() { stopped <- struct{}{} }}, nil
	}
	finished := make(chan error, 1)
	go func() {
		finished <- runGuest(ctx, a, map[string]string{controlPort: "c", agentPort: "a", viewPort: "v"}, ops)
	}()
	io.WriteString(b, `{"SRF_PROGRAM":"notepad","SRF_WINDOWS":"1","SRF_TILES":"[{\"id\":\"w1\",\"x\":0,\"y\":0,\"w\":400,\"h\":300}]","NO_AT_BRIDGE":"1"}`+"\n")
	expected := "SRF1 OUT xpra layer up\nSRF1 OUT PROGRAM_READY {\"windows\":[]}\n"
	out := make([]byte, len(expected))
	if _, err := io.ReadFull(b, out); err != nil {
		t.Fatal(err)
	}
	if string(out) != expected {
		t.Fatalf("%q", out)
	}
	io.WriteString(b, "SRF1 stop\n")
	if err := <-finished; err != nil {
		t.Fatal(err)
	}
	if len(commands) != 2 {
		t.Fatal(commands)
	}
	for _, c := range commands {
		if c.UID != 10001 || c.GID != 10001 {
			t.Fatal(c)
		}
	}
	if !reflect.DeepEqual(commands[0].Args, []string{"--port", "a", "--view-port", "v"}) {
		t.Fatal(commands[0])
	}
	if len(commands[1].Args) != 0 {
		t.Fatal("boot must invoke program entrypoint without arguments")
	}
	if len(stopped) != 2 {
		t.Fatalf("stopped %d", len(stopped))
	}
}
func fakeBootOps() bootOps {
	return bootOps{
		profile: func() (string, error) { return "/fake/labelled-profile", nil },
		mkdir:   func(string, os.FileMode) error { return nil }, chown: func(string, int, int) error { return nil }, chmod: func(string, os.FileMode) error { return nil }, mount: func(string, string, string, string) error { return nil }, check: func([]string) error { return nil },
	}
}

func TestDeadControlAndInvalidBootDoNotSpawn(t *testing.T) {
	for _, data := range []string{"", "{}\n", strings.Repeat("x", bootLineLimit+1) + "\n"} {
		a, b := net.Pipe()
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		ops := fakeBootOps()
		ops.start = func(commandSpec) (childProcess, error) { t.Error("unexpected spawn"); return childProcess{}, nil }
		go func() { io.WriteString(b, data); b.Close() }()
		if err := runGuest(ctx, a, map[string]string{controlPort: "c", agentPort: "a", viewPort: "v"}, ops); err == nil {
			t.Fatal("accepted invalid/dead control")
		}
		cancel()
	}
}

func TestLineRelayChunkingAndLimit(t *testing.T) {
	var out strings.Builder
	relay := &lineRelay{w: &out}
	for _, p := range []string{"xpra ", "layer up\nPROGRAM_", "READY {}\n"} {
		if _, err := io.WriteString(relay, p); err != nil {
			t.Fatal(err)
		}
	}
	if got := out.String(); got != "SRF1 OUT xpra layer up\nSRF1 OUT PROGRAM_READY {}\n" {
		t.Fatal(got)
	}
	if _, err := relay.Write([]byte(strings.Repeat("x", bootLineLimit+1))); err == nil {
		t.Fatal("unbounded output accepted")
	}
}

func TestPartialLaunchFailureCleansUp(t *testing.T) {
	a, b := net.Pipe()
	defer b.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	stopped := false
	count := 0
	ops := fakeBootOps()
	ops.start = func(commandSpec) (childProcess, error) {
		count++
		if count == 2 {
			return childProcess{}, errors.New("no program")
		}
		return childProcess{done: make(chan error), stop: func() { stopped = true }}, nil
	}
	go io.WriteString(b, `{"SRF_PROGRAM":"notepad","SRF_WINDOWS":"1","SRF_TILES":"[{\"id\":\"w1\",\"x\":0,\"y\":0,\"w\":400,\"h\":300}]","NO_AT_BRIDGE":"1"}`+"\n")
	if err := runGuest(ctx, a, map[string]string{controlPort: "c", agentPort: "a", viewPort: "v"}, ops); err == nil {
		t.Fatal("accepted failed child")
	}
	if !stopped {
		t.Fatal("agent left running")
	}
}

func TestBootReadCancellation(t *testing.T) {
	a, b := net.Pipe()
	defer b.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := runGuest(ctx, a, map[string]string{controlPort: "c", agentPort: "a", viewPort: "v"}, fakeBootOps()); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
}
