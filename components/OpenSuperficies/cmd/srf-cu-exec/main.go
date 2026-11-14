// srf-cu-exec: validate / run (fake world) / replay vqa.act-plan/v1 plans.
// Exit codes come only from src/cubook/rc (0,2..7; never 1).
package main

import (
	"flag"
	"fmt"
	"io"
	"os"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/ajson"
	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/prodgate"
	"opensuperficies.local/srf/src/cubook/rc"
)

// newDeps is the production effect set; tests replace it so no X server is touched.
var newDeps = func(env func(string) string) prodgate.Deps { return prodgate.Real(env) }

func main() { os.Exit(run(os.Args[1:], os.Stdout, os.Stderr)) }

func fail(stderr io.Writer, err error) int {
	fmt.Fprintln(stderr, "srf-cu-exec:", err)
	return rc.Of(err)
}

func usage(stderr io.Writer) int {
	fmt.Fprintln(stderr, "usage: srf-cu-exec validate --plan P | replay --plan P --steps S --frames F [--out O] | run --plan P --out S [--deny D] [--stop-file X] [--mock] (--fb F --ramws-dir D --iso I | OS_CU_TEST=1 --mock --frames F)")
	return rc.Usage
}

func loadPlan(path string) (*action.Plan, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, rc.New("E_IO", err.Error())
	}
	return action.DecodePlan(b)
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		return usage(stderr)
	}
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	plan := fs.String("plan", "", "")
	steps := fs.String("steps", "", "")
	frames := fs.String("frames", "", "")
	out := fs.String("out", "", "")
	deny := fs.String("deny", "", "")
	stop := fs.String("stop-file", "", "")
	mock := fs.Bool("mock", false, "")
	fb := fs.String("fb", "", "")
	ramDir := fs.String("ramws-dir", "", "")
	iso := fs.String("iso", "", "")
	prereg := fs.String("prereg", "", "")
	item := fs.String("item", "", "")
	start := fs.String("start", "", "")
	startWait := fs.Int("start-wait-ms", 30000, "")
	if err := fs.Parse(args[1:]); err != nil {
		return usage(stderr)
	}
	if args[0] == "run" && *prereg != "" {
		return runWL(wlArgs{prereg: *prereg, item: *item, start: *start, out: *out, stop: *stop, iso: *iso, plan: *plan,
			frames: *frames, mock: *mock, startWaitMS: *startWait}, stdout, stderr)
	}
	if *plan == "" {
		return usage(stderr)
	}
	p, err := loadPlan(*plan)
	if err != nil {
		return fail(stderr, err)
	}
	switch args[0] {
	case "validate":
		fmt.Fprintf(stdout, "ok actions=%d\n", len(p.Actions))
		return rc.OK
	case "replay":
		if *steps == "" || *frames == "" {
			return usage(stderr)
		}
		st, err := ajson.ReadAll(*steps)
		if err != nil {
			return fail(stderr, err)
		}
		fb, err := os.ReadFile(*frames)
		if err != nil {
			return fail(stderr, rc.New("E_IO", err.Error()))
		}
		fr, err := exec.ParseFrames(fb)
		if err != nil {
			return fail(stderr, err)
		}
		o, err := exec.Replay(p, st, fr)
		if err != nil {
			return fail(stderr, err)
		}
		if *out != "" {
			if err := os.WriteFile(*out, o, 0o600); err != nil {
				return fail(stderr, rc.New("E_IO", err.Error()))
			}
		} else {
			stdout.Write(o)
		}
		return rc.OK
	case "run":
		if *out == "" {
			return usage(stderr)
		}
		cfg := exec.Config{Mock: *mock, StopFile: *stop}
		var err error
		if *deny != "" {
			if cfg.Deny, err = exec.LoadDenyList(*deny); err != nil {
				return fail(stderr, err)
			}
		}
		// --iso is the expected VQA_ISO; every other variable comes from the process env.
		env := func(k string) string {
			if k == "VQA_ISO" && *iso != "" {
				return *iso
			}
			return os.Getenv(k)
		}
		deps := newDeps(env)
		mode, err := prodgate.Check(deps.Env, deps.Probe, deps.Iso)
		if err != nil {
			return fail(stderr, err)
		}
		var e *exec.Executor
		w, err := ajson.Open(*out)
		if err != nil {
			return fail(stderr, err)
		}
		defer w.Close()
		if mode == prodgate.TestFake {
			if *frames == "" || !*mock {
				return usage(stderr)
			}
			seq, err := loadFrameSeq(*frames)
			if err != nil {
				return fail(stderr, err)
			}
			e = exec.New(cfg, &exec.FakeActuator{}, &exec.SeqObserver{Frames: seq}, exec.XYLocator{}, w)
		} else {
			if *frames != "" || *fb == "" || *ramDir == "" {
				return usage(stderr)
			}
			e, closeAll, err := prodgate.Build(deps, prodgate.Args{FB: *fb, RamwsDir: *ramDir, Deny: cfg.Deny, Cfg: cfg})
			if err != nil {
				return fail(stderr, err)
			}
			defer closeAll()
			e.Log = w
			r, _, err := e.Run(p)
			if err != nil {
				return fail(stderr, err)
			}
			return r
		}
		r, _, err := e.Run(p)
		if err != nil {
			return fail(stderr, err)
		}
		return r
	}
	return usage(stderr)
}

func loadFrameSeq(path string) ([]exec.Frame, error) {
	fb, err := os.ReadFile(path)
	if err != nil {
		return nil, rc.New("E_IO", err.Error())
	}
	fm, err := exec.ParseFrames(fb)
	if err != nil {
		return nil, err
	}
	var seq []exec.Frame
	for ri := uint32(0); len(seq) < len(fm); ri++ {
		if f, ok := fm[ri]; ok {
			seq = append(seq, f)
		} else if ri > uint32(len(fm))+1 {
			return nil, rc.New("E_SCHEMA", "frames must have contiguous ri from 0 or 1")
		}
	}
	return seq, nil
}
