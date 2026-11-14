// Command srf-cu-mcp is the CU MCP server (stdio JSON-RPC 2.0). Exit codes
// follow SPEC_INT R4 via src/cubook/rc: 0, 2..7, never 1.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/mcp"
	"opensuperficies.local/srf/src/cubook/prodgate"
	"opensuperficies.local/srf/src/cubook/rc"
)

type multi []string

func (m *multi) String() string     { return strings.Join(*m, ",") }
func (m *multi) Set(v string) error { *m = append(*m, v); return nil }

// newDeps is the production effect set; tests replace it so no X server is touched.
var newDeps = func(env func(string) string) prodgate.Deps { return prodgate.Real(env) }

func main() { os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr)) }

func run(args []string, in io.Reader, out, errw io.Writer) int {
	fs := flag.NewFlagSet("srf-cu-mcp", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var fakes multi
	registry := fs.String("registry", "", "registry.json path")
	casDir := fs.String("cas-dir", "", "CAS directory (temp in tests, <out>/cas in runs)")
	scratch := fs.String("scratch-dir", "", "capsule scratch dir for Godot projects")
	outDir := fs.String("out-dir", "", "allowed out_dir for scripts")
	confirm := fs.Bool("confirm-destructive", false, "allow unlisted write/irrev downstream tools")
	mockGUI := fs.Bool("mock-gui", false, "TEST ONLY: GUI actions are accepted but stay UNVERIFIED (no verifier)")
	l2Fake := fs.Bool("l2-fake", false, "TEST ONLY: route GUI actions through the L2 executor over in-memory fakes (no X11)")
	denyList := fs.String("deny-list", "", "act_deny.tsv for the L2 executor (required with --l2-fake)")
	fb := fs.String("fb", "", "Xvfb_screen0 path (production GUI executor)")
	ramDir := fs.String("ramws-dir", "", "ramws run directory (production GUI executor)")
	iso := fs.String("iso", "", "expected VQA_ISO (production GUI executor)")
	astScript := fs.String("ast-script", "capsule/astcheck/astcheck.py", "in-capsule astcheck.py path (production script gate)")
	fs.Var(&fakes, "fake", "fake downstream fixture (repeatable); no sockets")
	if err := fs.Parse(args); err != nil || *registry == "" || *casDir == "" {
		fmt.Fprintln(errw, "usage: srf-cu-mcp --registry R --cas-dir D [--fake F]... [--scratch-dir S] [--confirm-destructive]")
		return rc.Usage
	}
	// Test-only flags exist only under OS_CU_TEST=1; a production run refuses them.
	if (*mockGUI || *l2Fake || len(fakes) > 0) && os.Getenv("OS_CU_TEST") != "1" {
		fmt.Fprintln(errw, "E_USAGE: --mock-gui/--l2-fake/--fake are test-only (OS_CU_TEST=1)")
		return rc.Usage
	}
	if *l2Fake && *denyList == "" {
		fmt.Fprintln(errw, "E_USAGE: --l2-fake needs --deny-list")
		return rc.Usage
	}
	reg, err := mcp.LoadRegistry(*registry)
	if err != nil {
		fmt.Fprintln(errw, err)
		return mcp.RcOf(err)
	}
	var caller mcp.Caller = mcp.HTTPCaller{}
	if len(fakes) > 0 {
		fc, err := mcp.NewFakeCaller(fakes...)
		if err != nil {
			fmt.Fprintln(errw, err)
			return mcp.RcOf(err)
		}
		caller = fc
	}
	var gui mcp.Executor = mcp.Unwired{}
	var astGate mcp.ASTChecker
	switch {
	case *l2Fake:
		e, err := fakeL2(*denyList)
		if err != nil {
			fmt.Fprintln(errw, err)
			return mcp.RcOf(err)
		}
		gui = unverified{mcp.L2Bridge{E: e}}
	case *mockGUI:
		gui = mcp.MockGUI{}
	default:
		deps := newDeps(envWithIso(*iso))
		g, closeAll, err := prodGUI(deps, *fb, *ramDir, *denyList, errw)
		if err != nil {
			fmt.Fprintln(errw, err)
			return mcp.RcOf(err)
		}
		defer closeAll()
		gui = g
		// RJ1-02 / ADW2-03: the script gate depends on the capsule probe only, not on the X11
		// prodgate verdict, so a capsule with GUI unwired still checks every live script.
		if capture.InCapsule(deps.Probe) {
			astGate = astGateFor(deps, *astScript)
		}
	}
	cas := &mcp.CAS{Dir: *casDir}
	d := &mcp.Dispatcher{Reg: reg, Caller: caller, CAS: cas, GUI: gui, ConfirmDestructive: *confirm, ScratchDir: *scratch, OutDir: *outDir, ASTGate: astGate}
	if err := mcp.NewServer(mcp.Config{Exec: d, CAS: cas}).Serve(in, out); err != nil {
		fmt.Fprintln(errw, err)
		return rc.Usage
	}
	return rc.OK
}

func envWithIso(iso string) func(string) string {
	return func(k string) string {
		if k == "VQA_ISO" && iso != "" {
			return iso
		}
		return os.Getenv(k)
	}
}

// prodGUI wires the real L2 executor (XTestActuator + capture Snapper) only when
// prodgate says Prod; any refusal keeps mcp.Unwired, so the server still starts
// and every GUI action stops with gui_executor_unwired.
func prodGUI(d prodgate.Deps, fb, ramDir, denyPath string, errw io.Writer) (mcp.Executor, func(), error) {
	noop := func() {}
	m, err := prodgate.Check(d.Env, d.Probe, d.Iso)
	if err != nil || m != prodgate.Prod {
		return mcp.Unwired{}, noop, nil
	}
	if fb == "" || ramDir == "" || denyPath == "" {
		return nil, noop, rc2("production GUI executor needs --fb, --ramws-dir and --deny-list")
	}
	deny, err := exec.LoadDenyList(denyPath)
	if err != nil {
		return nil, noop, err
	}
	e, closeAll, err := prodgate.Build(d, prodgate.Args{FB: fb, RamwsDir: ramDir, Deny: deny})
	if err != nil {
		fmt.Fprintln(errw, err)
		return mcp.Unwired{}, noop, nil
	}
	return mcp.L2Bridge{E: e}, closeAll, nil
}

// astGateFor wires the in-capsule AST gate: ExecRunner on the capsule checker path, run only when the
// capsule probe holds. Without a script path the gate stays nil, so script tools stay unwired.
func astGateFor(d prodgate.Deps, script string) mcp.ASTChecker {
	if script == "" {
		return nil
	}
	return &mcp.ASTGate{
		Runner:    mcp.ExecRunner{Script: script},
		InCapsule: func() bool { return capture.InCapsule(d.Probe) },
	}
}

func rc2(msg string) error { return rc.New("E_USAGE", msg) }

// unverified caps every verdict of a fake executor: fabricated frames are no
// evidence, so EFFECT_OK can never come out of them.
type unverified struct{ mcp.Executor }

func (u unverified) Execute(ctx context.Context, a mcp.Action) (mcp.Outcome, error) {
	o, err := u.Executor.Execute(ctx, a)
	downgrade := func(st *mcp.Step) {
		if st.Verdict == "EFFECT_OK" {
			st.Verdict = "UNVERIFIED"
		}
	}
	downgrade(&o.Step)
	for i := range o.FallbackSteps {
		downgrade(&o.FallbackSteps[i])
	}
	return o, err
}

// fakeL2 is the L2 executor over a recording actuator and a changing frame
// sequence, with the deny list loaded: the production wiring (XTestActuator +
// capture.Snapper) is S-LIVE.
func fakeL2(denyPath string) (*exec.Executor, error) {
	deny, err := exec.LoadDenyList(denyPath)
	if err != nil {
		return nil, err
	}
	var fr []exec.Frame
	for i := 0; i < 64; i++ {
		fr = append(fr, exec.MkFrame(uint32(i+1), "00000000000000aa", strings.Repeat("p", i+1), 64, 64))
	}
	return exec.New(exec.Config{Deny: deny}, &exec.FakeActuator{}, &exec.SeqObserver{Frames: fr}, exec.XYLocator{}, nil), nil
}
