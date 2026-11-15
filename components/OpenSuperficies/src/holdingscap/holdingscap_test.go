package holdingscap

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

type fake struct {
	calls [][]string
	reply func(argv []string) ([]byte, error)
}

func (f *fake) Run(_ context.Context, argv []string, _ []byte) ([]byte, error) {
	f.calls = append(f.calls, argv)
	return f.reply(argv)
}

func tool(f *fake, t *testing.T) Tool {
	n := uint64(0)
	return Tool{R: f, Clock: func() uint64 { n += 1e6; return n }, Root: t.TempDir(), Sleep: func(time.Duration) {}}
}

const cont = "srf-ng-hcap-hc-0123456789ab-1"

func TestRunArgsKeepIsolation(t *testing.T) {
	a := RunArgs(cont, "/stage/app", "10-14")
	for _, want := range [][]string{{"--network", "none"}, {"--memory", "4g"}, {"--memory-swap", "4g"}, {"--cpuset-cpus", "10-14"}, {"--user", "10001:0"}} {
		i := slices.Index(a, want[0])
		if i < 0 || a[i+1] != want[1] {
			t.Fatalf("missing %v in %v", want, a)
		}
	}
	if !slices.Contains(a, "/stage/app:/opt/holdings:ro") || slices.Contains(a, "--privileged") {
		t.Fatal(a)
	}
	if !strings.Contains(LaunchCommand("h3-core-journey", 4242, ""), "--no-sandbox") {
		t.Fatal("D-AL1 test capsule launch")
	}
	if strings.Contains(LaunchCommand("h3-core-journey", 4242, ""), "--ui") || !strings.Contains(LaunchCommand("h3-core-journey", 4242, "v2"), " --ui v2 ") {
		t.Fatal("ui passthrough")
	}
}

func TestRefusals(t *testing.T) {
	f := &fake{reply: func([]string) ([]byte, error) { return nil, nil }}
	tl := tool(f, t)
	ctx := context.Background()
	if _, e := tl.Start(ctx, StartSpec{Slots: 1, AppDir: "/x", Scenario: "s", Seed: 1, UI: "v3"}); Code(e) != 2 {
		t.Fatal("ui outside lit|v2 must be refused", e)
	}
	if _, e := tl.Start(ctx, StartSpec{Slots: 5, AppDir: "/x", Scenario: "s", Seed: 1}); Code(e) != 2 {
		t.Fatal("slots ceiling", e)
	}
	if _, e := tl.Start(ctx, StartSpec{Slots: 1, AppDir: t.TempDir(), Scenario: "s", Seed: 1}); Code(e) != 4 {
		t.Fatal("unreadable app", e)
	}
	if _, e := tl.Frame(ctx, "srf-ng-holdings1", false); Code(e) != 2 {
		t.Fatal("foreign container", e)
	}
	if _, e := tl.Input(ctx, cont, Input{Key: "Tab;rm"}); Code(e) != 2 {
		t.Fatal("keysym injection", e)
	}
	if _, e := tl.Input(ctx, cont, Input{X: 1280, Y: 1, Button: 1}); Code(e) != 2 {
		t.Fatal("click outside", e)
	}
	if len(f.calls) != 0 {
		t.Fatalf("refusals must not reach docker: %v", f.calls)
	}
}

func TestInputReturnsInCapsuleStamp(t *testing.T) {
	f := &fake{reply: func(a []string) ([]byte, error) { return []byte("123456789\n"), nil }}
	r, e := tool(f, t).Input(context.Background(), cont, Input{Key: "Tab"})
	if e != nil || r.InputMonoNS != 123456789 || !strings.Contains(strings.Join(f.calls[0], " "), "'xdotool','key','--clearmodifiers','Tab'") {
		t.Fatalf("%+v %v %v", r, e, f.calls)
	}
}

func TestObserveRetriesTickWindowThenFailsClosed(t *testing.T) {
	n := 0
	f := &fake{reply: func([]string) ([]byte, error) {
		n++
		if n < 3 {
			return []byte(`{"verdict":"refused","code":"TICK_WINDOW"}`), nil
		}
		return []byte(`{"verdict":"ok","observation":{"tick":0,"state":{"cash_minor":9007199254740993},"state_sha256":"ab"}}`), nil
	}}
	o, e := tool(f, t).Observe(context.Background(), cont)
	if e != nil {
		t.Fatal(o, e)
	}
	// Integers above 2^53 must survive the relay byte-exactly (no float64 round trip).
	if b, _ := json.Marshal(o); string(b) != `{"tick":0,"state":{"cash_minor":9007199254740993},"state_sha256":"ab"}` {
		t.Fatalf("observation not relayed byte-exactly: %s", b)
	}
	f.reply = func([]string) ([]byte, error) { return []byte(`{"verdict":"ok"}`), nil }
	if _, e = tool(f, t).Observe(context.Background(), cont); Code(e) != 5 {
		t.Fatal("ok without observation must fail", e)
	}
	f.reply = func([]string) ([]byte, error) { return make([]byte, MaxLaneReply+1), nil }
	if _, e = tool(f, t).Observe(context.Background(), cont); Code(e) != 5 {
		t.Fatal("oversize reply must fail", e)
	}
	f.reply = func([]string) ([]byte, error) { return []byte(`{"verdict":"refused","code":"SCHEMA"}`), nil }
	if _, e = tool(f, t).Observe(context.Background(), cont); Code(e) != 5 {
		t.Fatal("other refusal must fail", e)
	}
}

func TestSplitPairRejectsBadFraming(t *testing.T) {
	leg := func(b []byte) []byte {
		h := make([]byte, 8)
		binary.BigEndian.PutUint64(h, uint64(len(b)))
		return append(h, b...)
	}
	ok := append(leg([]byte("a")), leg([]byte("b"))...)
	if _, e := splitPair(ok); e != nil {
		t.Fatal(e)
	}
	for _, bad := range [][]byte{ok[:5], append(ok, 0), leg([]byte("only-one"))} {
		if _, e := splitPair(bad); Code(e) != 5 {
			t.Fatal("bad framing accepted")
		}
	}
}

func TestStopVerifiesRemoval(t *testing.T) {
	f := &fake{reply: func(a []string) ([]byte, error) {
		if a[1] == "ps" {
			return []byte(cont + "\n"), nil
		}
		return nil, nil
	}}
	if e := tool(f, t).Stop(context.Background(), cont); Code(e) != 6 {
		t.Fatal("leftover must be indeterminate", e)
	}
	f.reply = func(a []string) ([]byte, error) {
		if a[1] == "rm" {
			return nil, errors.New("daemon down")
		}
		return nil, nil
	}
	if e := tool(f, t).Stop(context.Background(), cont); Code(e) != 6 {
		t.Fatal(e)
	}
}

func appDir(t *testing.T, dirMode os.FileMode) string {
	d := t.TempDir()
	if e := os.WriteFile(filepath.Join(d, "holdings-h2l"), []byte("#!/bin/sh\n"), 0o755); e != nil {
		t.Fatal(e)
	}
	if e := os.Chmod(d, dirMode); e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { _ = os.Chmod(d, 0o755) })
	return d
}

func TestStartRefusesPrivateAppFolder(t *testing.T) {
	f := &fake{reply: func([]string) ([]byte, error) { return nil, nil }}
	if _, e := tool(f, t).Start(context.Background(), StartSpec{Slots: 1, AppDir: appDir(t, 0o700), Scenario: "s", Seed: 1}); Code(e) != 4 {
		t.Fatal("0700 app folder must be refused before docker", e)
	}
	if len(f.calls) != 0 {
		t.Fatal("docker called", f.calls)
	}
}

func TestFailedStartRemovesItsCapsules(t *testing.T) {
	removed := map[string]bool{}
	f := &fake{reply: func(a []string) ([]byte, error) {
		switch {
		case len(a) > 2 && a[0] == "sh" && strings.Contains(a[2], "docker logs"):
			return []byte("xpra layer up\n"), nil // never HOLDINGS_READY
		case len(a) > 2 && a[0] == "docker" && a[1] == "rm":
			removed[a[3]] = true
		}
		return nil, nil
	}}
	caps, e := tool(f, t).Start(context.Background(), StartSpec{Slots: 2, AppDir: appDir(t, 0o755), Scenario: "s", Seed: 1})
	if Code(e) != 4 || caps != nil {
		t.Fatal("ready timeout must fail with no capsules returned", caps, e)
	}
	if len(removed) != 2 {
		t.Fatal("both created capsules must be removed", removed)
	}
}
