package main

import (
	"bytes"
	"context"
	"encoding/json"
	"opensuperficies.local/srf/src/browserid/store"
	"opensuperficies.local/srf/src/browserid/surface"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestCLIJSONRefusals(t *testing.T) {
	for _, args := range [][]string{nil, {"unknown"}, {"input", "--container", "srf-id-test", "--window", "w1", "--key", "Tab", "--type", "secret"}, {"input", "--click", "1,no"}, {"input", "--container", "srf-id-test", "--window", "w1", "--key", "Tab", "--paste"}, {"view", "--container", "srf-id-test", "--record", "demo-1"}, {"frame", "--unknown"}, {"login", "--container", "foreign"}} {
		var out, errb bytes.Buffer
		rc := run(args, &out, &errb, surface.Tool{Launcher: surface.UnavailableLauncher{}})
		if rc < 2 || rc > 7 {
			t.Fatal(rc)
		}
		var v map[string]any
		if e := json.Unmarshal(out.Bytes(), &v); e != nil {
			t.Fatal(args, e, out.String())
		}
		if bytes.Contains(out.Bytes(), []byte("secret")) {
			t.Fatal("payload leaked")
		}
	}
}
func TestCLIStartSeatRefusal(t *testing.T) {
	var out, errb bytes.Buffer
	rc := run([]string{"start", "--identity", "identity-a"}, &out, &errb, surface.Tool{Launcher: surface.UnavailableLauncher{}})
	if rc != 3 || !json.Valid(out.Bytes()) {
		t.Fatal(rc, out.String())
	}
}

type commandFake struct{ calls [][]string }

func (r *commandFake) Run(_ context.Context, a []string, _ []byte) ([]byte, error) {
	r.calls = append(r.calls, append([]string(nil), a...))
	return nil, nil
}
func TestCLINetworkDispatch(t *testing.T) {
	r := &commandFake{}
	root := t.TempDir()
	_ = os.Chmod(root, 0700)
	var out, errs bytes.Buffer
	if rc := run([]string{"net", "down", "--state-dir", root}, &out, &errs, surface.Tool{R: r}); rc != 0 {
		t.Fatal(rc, out.String())
	}
	if !json.Valid(out.Bytes()) || len(r.calls) == 0 {
		t.Fatal(out.String(), r.calls)
	}
	for _, args := range [][]string{{"net"}, {"net", "other"}, {"net", "down", "--proxy-bin", "/does-not-exist"}, {"login", "--container", "srf-id-test", "--port", "-1"}} {
		out.Reset()
		before := len(r.calls)
		if rc := run(args, &out, &errs, surface.Tool{R: r, Root: root}); rc != 2 {
			t.Fatal(args, rc, out.String())
		}
		if len(r.calls) != before {
			t.Fatal("invalid flags reached runner")
		}
	}
}

func TestCLIAXFlags(t *testing.T) {
	for _, tc := range []struct {
		args []string
		code int
	}{
		{[]string{"ax", "--container", "srf-id-test", "--window", "w1"}, 3},
		{[]string{"ax", "--container", "srf-id-test", "--window", "w1", "--text-container", "ab12"}, 3},
		{[]string{"ax", "--container", "srf-id-test"}, 2},
		{[]string{"ax", "--window", "w1"}, 2},
		{[]string{"ax", "--container", "srf-id-test", "--window", "w1", "--key", "Tab"}, 2},
		{[]string{"frame", "--container", "srf-id-test", "--window", "w1", "--text-container", "ab12"}, 2},
	} {
		t.Run(tc.args[0]+"-"+tc.args[len(tc.args)-1], func(t *testing.T) {
			t.Setenv("SRF_IDENTITY_STORE_ROOT", "")
			t.Setenv("SRF_PROXY_REGISTRY", "")
			r := &commandFake{}
			var out, errs bytes.Buffer
			if rc := run(tc.args, &out, &errs, surface.Tool{R: r}); rc != tc.code {
				t.Fatalf("rc %d expected %d: %s", rc, tc.code, out.String())
			}
			if len(r.calls) != 0 || errs.Len() != 0 || !json.Valid(out.Bytes()) || bytes.Count(out.Bytes(), []byte("\n")) != 1 {
				t.Fatal("invalid CLI refusal")
			}
		})
	}
}

func TestCLIViewFlags(t *testing.T) {
	for _, tc := range []struct {
		args []string
		code int
	}{
		{[]string{"view", "--container", "foreign"}, 2},
		{[]string{"view", "--container", "srf-id-test", "--port", "-1"}, 2},
		{[]string{"view", "--container", "srf-id-test", "--port", "65536"}, 2},
		{[]string{"view", "--container", "srf-id-test", "--login"}, 3},
		{[]string{"view", "--container", "srf-id-test"}, 3},
		{[]string{"login", "--container", "srf-id-test", "--login"}, 2},
		{[]string{"frame", "--container", "srf-id-test", "--login"}, 2},
	} {
		var out, errs bytes.Buffer
		if rc := run(tc.args, &out, &errs, surface.Tool{}); rc != tc.code || !json.Valid(out.Bytes()) {
			t.Fatal(tc.args, rc, out.String())
		}
	}
}

func TestCLIProgramFlags(t *testing.T) {
	for _, flags := range [][]string{
		{"--program", "notepad"}, {"--kind", "program"}, {"--kind", "other"},
		{"--kind", "browser", "--program", "notepad"},
		{"--kind", "program", "--program", "notepad", "--capsule", "srf-id-wrong"},
	} {
		r := &commandFake{}
		var out, errs bytes.Buffer
		args := append([]string{"start", "--identity", "identity-a"}, flags...)
		if rc := run(args, &out, &errs, surface.Tool{R: r, Launcher: surface.UnavailableLauncher{}}); rc != 2 || len(r.calls) != 0 {
			t.Fatal(args, rc, out.String())
		}
	}
	var out, errs bytes.Buffer
	if rc := run([]string{"start", "--identity", "identity-a", "--kind", "program", "--program", "notepad", "--capsule", "srf-pg-valid"}, &out, &errs, surface.Tool{Launcher: surface.UnavailableLauncher{}}); rc != 3 {
		t.Fatal(rc, out.String())
	}
}

// Exercise the command encoder as well as the surface structs: newline and key
// order are part of the browser adapter contract.
func TestCLIBrowserWireGoldens(t *testing.T) { testCLIWireGoldens(t, false) }
func TestCLIProgramWireGoldens(t *testing.T) { testCLIWireGoldens(t, true) }
func testCLIWireGoldens(t *testing.T, program bool) {
	root := t.TempDir()
	if e := os.Chmod(root, 0700); e != nil {
		t.Fatal(e)
	}
	st, e := store.New(filepath.Join(root, "store"))
	if e != nil {
		t.Fatal(e)
	}
	browser, capsule := "chromium", "srf-id-test"
	if program {
		browser, capsule = "program", "srf-pg-test"
	}
	if e = st.Create(store.Identity{ProfileID: "identity-a", Browser: browser, AccountLabel: "fixture", AllowedDomains: []string{"example.org"}}); e != nil {
		t.Fatal(e)
	}
	runner := &wireRunner{program: program}
	tool := surface.Tool{R: runner, Clock: func() uint64 { return 42 }, Root: filepath.Join(root, "state"), Store: st, StoreRoot: filepath.Join(root, "store"), Registry: filepath.Join(root, "registry.json"), Proxy: surface.ProxyURL, Launcher: wireLaunch{}, ProfileCheck: func(string) error { return nil }}
	var out, errs bytes.Buffer
	startArgs := []string{"start", "--identity", "identity-a", "--capsule", capsule}
	if program {
		startArgs = append(startArgs, "--kind", "program", "--program", "notepad")
	}
	if rc := run(startArgs, &out, &errs, tool); rc != 0 {
		t.Fatal(rc, out.String())
	}
	want := `{"container":"srf-id-test","run_id":"srf-id-test","windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"title":"","url":""}]}` + "\n"
	if program {
		want = strings.Replace(want, `{"container":`, `{"kind":"program","container":`, 1)
		want = strings.ReplaceAll(want, "srf-id-test", capsule)
	}
	if out.String() != want {
		t.Fatal("start wire", out.String())
	}
	for _, tc := range []struct {
		kind  string
		flags []string
	}{
		{"key", []string{"--key", "Tab"}}, {"click", []string{"--click", "10,20"}},
		{"text", []string{"--type", "synthetic fixture"}}, {"scroll", []string{"--scroll", "10,20", "--dir", "down", "--count", "2"}},
		{"drag", []string{"--drag", "10,20:30,40"}},
		{"text", []string{"--type-stdin"}},
		{"text", []string{"--type-stdin", "--paste"}},
	} {
		t.Run(strings.Join(tc.flags, " "), func(t *testing.T) {
			if slices.Contains(tc.flags, "--type-stdin") {
				f, e := os.CreateTemp(t.TempDir(), "stdin")
				if e != nil {
					t.Fatal(e)
				}
				defer f.Close()
				if _, e = f.WriteString("synthetic fixture"); e != nil {
					t.Fatal(e)
				}
				if _, e = f.Seek(0, 0); e != nil {
					t.Fatal(e)
				}
				old := os.Stdin
				os.Stdin = f
				defer func() { os.Stdin = old }()
			}
			out.Reset()
			before := len(runner.calls)
			args := append([]string{"input", "--container", capsule, "--window", "w1"}, tc.flags...)
			if rc := run(args, &out, &errs, tool); rc != 0 {
				t.Fatal(rc, out.String())
			}
			want := `{"surface_id":"w1","kind":"` + tc.kind + `","input_mono_ns":42,"focus_verified":true,"result":"ok"}` + "\n"
			if out.String() != want {
				t.Fatal("input wire", out.String())
			}
			if slices.Contains(tc.flags, "--type-stdin") {
				found := false
				for i := before; i < len(runner.calls); i++ {
					argv := strings.Join(runner.calls[i], " ")
					if strings.Contains(argv, "synthetic fixture") {
						t.Fatal("stdin text in argv")
					}
					if string(runner.inputs[i]) == "synthetic fixture" {
						found = true
						if slices.Contains(tc.flags, "--paste") {
							if !strings.Contains(argv, "SELECTION_CLIPBOARD") {
								t.Error("paste did not use clipboard")
							}
						} else if !strings.Contains(argv, "xdotool type --clearmodifiers --delay 0 --file -") {
							t.Error("stdin did not use typing helper")
						}
					}
				}
				if !found {
					t.Error("stdin text was not delivered")
				}
			}
		})
	}
}

type wireLaunch struct{}

func (wireLaunch) Available() error                            { return nil }
func (wireLaunch) Start(context.Context, surface.Launch) error { return nil }
func (wireLaunch) Remove(context.Context, string) error        { return nil }

type wireRunner struct {
	program bool
	calls   [][]string
	inputs  [][]byte
}

func (r *wireRunner) Run(_ context.Context, a []string, in []byte) ([]byte, error) {
	r.calls = append(r.calls, slices.Clone(a))
	r.inputs = append(r.inputs, slices.Clone(in))
	s := strings.Join(a, " ")
	switch {
	case strings.Contains(s, "network inspect"):
		return []byte(`{"Name":"srf-idnet","Internal":true,"IPAM":{"Config":[{"Subnet":"172.30.80.0/24"}]}}`), nil
	case strings.Contains(s, "{{.State.Running}}"):
		return []byte("true\n"), nil
	case strings.Contains(s, "inspect"):
		return []byte("172.30.80.10\n"), nil
	case strings.Contains(s, "logs"):
		if r.program {
			return []byte("xpra layer up\n" + `PROGRAM_READY {"windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"xid":"0x123"}]}`), nil
		}
		return []byte("xpra layer up\n" + `IDENTITY_READY {"windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"xid":"0x123"}]}`), nil
	case strings.Contains(s, "getwindowgeometry"):
		return []byte("WINDOW=291\nX=0\nY=0\nWIDTH=1280\nHEIGHT=900\nSCREEN=0\n"), nil
	case strings.Contains(s, "getwindowfocus"):
		return []byte("291\n"), nil
	}
	return nil, nil
}

func TestCapabilitiesContractV1(t *testing.T) {
	var out, errOut bytes.Buffer
	if rc := run([]string{"capabilities"}, &out, &errOut, surface.Tool{}); rc != 0 || errOut.Len() != 0 {
		t.Fatal(rc, errOut.String())
	}
	if out.String() != "srf-identity-surface capabilities v1\n--paste --type-stdin --scroll --drag downloads net_modes=legacy,open,proxied\n" {
		t.Fatalf("%q", out.String())
	}
	out.Reset()
	if rc := run([]string{"capabilities", "--container", "x"}, &out, &errOut, surface.Tool{}); rc != 2 {
		t.Fatal("capabilities must take no arguments")
	}
}

// Net mode refusals are code 2 before the launcher (whose unavailability is code 3) and any docker call.
func TestCLINetModeFlags(t *testing.T) {
	for _, tc := range []struct {
		flags []string
		code  int
	}{
		{[]string{"--net", "legacy"}, 3},
		{[]string{"--net", "open"}, 3},
		{[]string{"--net", "proxied", "--policy", "/p.json", "--proxy-bin", "/bin/p"}, 3},
		{[]string{"--net", "proxied", "--policy", "/p.json", "--proxy-bin", "/bin/p", "--audit"}, 3},
		{[]string{"--net", "bridge"}, 2},
		{[]string{"--net", ""}, 2},
		{[]string{"--net", "open", "--kind", "program", "--program", "notepad"}, 2},
		{[]string{"--net", "legacy", "--kind", "vm", "--program", "notepad-vm"}, 2},
		{[]string{"--policy", "/p.json"}, 2},
		{[]string{"--audit"}, 2},
		{[]string{"--proxy-bin", "/bin/p"}, 2},
		{[]string{"--net", "open", "--audit"}, 2},
		{[]string{"--net", "proxied", "--policy", "/p.json"}, 2},
		{[]string{"--net", "proxied", "--proxy-bin", "/bin/p"}, 2},
		{[]string{"--net", "proxied", "--policy", "/p.json", "--proxy-bin", "/bin/p", "--audit=false"}, 2},
		{[]string{"--audit-dir", "/a"}, 2},
	} {
		var out, errs bytes.Buffer
		r := &commandFake{}
		args := append([]string{"start", "--identity", "identity-a"}, tc.flags...)
		rc := run(args, &out, &errs, surface.Tool{R: r, Launcher: surface.UnavailableLauncher{}})
		if rc != tc.code || len(r.calls) != 0 || !json.Valid(out.Bytes()) {
			t.Errorf("%v rc=%d want=%d %s", tc.flags, rc, tc.code, out.String())
		}
	}
	for _, verb := range [][]string{{"stop", "--container", "srf-id-test", "--net", "open"}, {"net", "up", "--net", "open"}, {"net", "up", "--audit"}} {
		var out, errs bytes.Buffer
		if rc := run(verb, &out, &errs, surface.Tool{Launcher: surface.UnavailableLauncher{}}); rc != 2 {
			t.Errorf("%v rc=%d", verb, rc)
		}
	}
}

func TestCLIVMKindValidation(t *testing.T) {
	for _, tc := range []struct {
		flags []string
		code  int
	}{
		{[]string{"--kind", "vm", "--program", "notepad-vm", "--capsule", "srf-vm-test"}, 3},
		{[]string{"--kind", "vm"}, 2},
		{[]string{"--kind", "vm", "--program", "notepad-vm", "--capsule", "srf-pg-test"}, 2},
	} {
		var out, errs bytes.Buffer
		args := append([]string{"start", "--identity", "identity-a"}, tc.flags...)
		rc := run(args, &out, &errs, surface.Tool{Launcher: surface.UnavailableLauncher{}})
		if rc != tc.code {
			t.Errorf("%v rc=%d want=%d %s", tc.flags, rc, tc.code, out.String())
		}
	}
}

func TestCLIRuntimeKindMismatch(t *testing.T) {
	root := t.TempDir()
	st, e := store.New(filepath.Join(root, "store"))
	if e != nil {
		t.Fatal(e)
	}
	runner := &commandFake{}
	tool := surface.Tool{R: runner, Clock: func() uint64 { return 42 }, Root: filepath.Join(root, "state"), Store: st, StoreRoot: filepath.Join(root, "store"), Registry: filepath.Join(root, "registry.json"), Launcher: wireLaunch{}}
	for _, tc := range []struct{ kind, program, capsule string }{
		{"program", "notepad-vm", "srf-pg-test"}, {"vm", "notepad", "srf-vm-test"},
	} {
		var out, errs bytes.Buffer
		rc := run([]string{"start", "--identity", "identity-a", "--kind", tc.kind, "--program", tc.program, "--capsule", tc.capsule}, &out, &errs, tool)
		if rc != 2 || len(runner.calls) != 0 {
			t.Fatal(tc, rc, out.String())
		}
	}
}
